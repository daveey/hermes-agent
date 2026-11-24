"""A live session's stored turn results lose their transcript copy at the exact-retry horizon."""
import json
import time

from gateway.session_results import admission_result, retain_result
from hermes_state import SessionDB
from hermes_state_mutation_retirement import RETRY_HORIZON_S
from hermes_state_result_horizon import compact_settled_results
from hermes_state_terminal import RESULT_PREFIX
import hermes_state_runtime as rt

TURN = [{'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'c1'}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'x' * 5000},
        {'role': 'assistant', 'content': 'the reply'}]


def _settle(db, epoch, request, session='chat', reply='the reply'):
    rt.admit_session_input(db, epoch=epoch, principal_id='api', session_id=session, request_id=request,
                           payload={'text': 'question ' + request})
    row = rt.claim_session_input(db, epoch=epoch, session_id=session)
    result = {'result': {'final_response': reply, 'messages': [dict(m) for m in TURN], 'completed': True,
                         'last_reasoning': 'thinking ' * 50},
              'usage': {'input_tokens': 7, 'output_tokens': 3}}
    retain_result(db, epoch=epoch, row=row, result=result)
    return row['admission_id'], result


def _settled_at(db, admission_id):
    with db._read_ctx() as c:
        return c.execute('SELECT settled_at FROM session_admissions WHERE admission_id=?', (admission_id,)).fetchone()[0]


def _age(db, admission_id, seconds):
    db._execute_write(lambda c: c.execute('UPDATE session_admissions SET settled_at=? WHERE admission_id=?',
                                          (time.time() - seconds, admission_id)))


def _size(db, admission_id):
    with db._read_ctx() as c:
        return len(c.execute('SELECT value FROM state_meta WHERE key=?', (RESULT_PREFIX + admission_id,)).fetchone()[0])


def _db(tmp_path):
    db = SessionDB(tmp_path / 'state.db')
    db.create_session('chat', source='api_server')
    return db, rt.begin_runtime_epoch(db, instance_id='owner')


def test_settling_an_admission_stamps_when(tmp_path):
    db, epoch = _db(tmp_path)
    with db:
        before = time.time()
        admission_id, _ = _settle(db, epoch, 'one')
        assert before - 2 <= _settled_at(db, admission_id) <= time.time() + 2
        rt.admit_session_input(db, epoch=epoch, principal_id='api', session_id='chat', request_id='queued',
                               payload={'text': 'not yet run'})
        with db._read_ctx() as c:
            assert c.execute("SELECT settled_at FROM session_admissions WHERE request_id='queued'").fetchone()[0] is None


def test_a_result_past_the_horizon_keeps_its_outcome_and_drops_the_transcript_copy(tmp_path):
    db, epoch = _db(tmp_path)
    with db:
        old, result = _settle(db, epoch, 'old')
        young, _ = _settle(db, epoch, 'young')
        _age(db, old, RETRY_HORIZON_S + 60)
        _age(db, young, RETRY_HORIZON_S - 60)
        big, young_stored = _size(db, old), admission_result(db, young)
        assert len(young_stored['result']['messages']) == 3
        assert db._execute_write(compact_settled_results) == 1
        kept = admission_result(db, old)
        assert kept['result']['final_response'] == 'the reply' and kept['result']['completed'] is True
        assert kept['result']['messages'] == [] and 'last_reasoning' not in kept['result']
        assert kept['usage'] == result['usage']
        assert _size(db, old) < big / 10
        assert admission_result(db, young) == young_stored  # inside the horizon: byte-exact
        assert db._execute_write(compact_settled_results) == 0  # nothing left to do: a compacted row is not redone


def test_a_result_settled_before_the_stamp_existed_ages_from_the_first_pass(tmp_path):
    db, epoch = _db(tmp_path)
    with db:
        legacy, _ = _settle(db, epoch, 'legacy')
        db._execute_write(lambda c: c.execute('UPDATE session_admissions SET settled_at=NULL'))
        stored, started = admission_result(db, legacy), time.time()
        assert db._execute_write(compact_settled_results) == 0
        assert admission_result(db, legacy) == stored and len(stored['result']['messages']) == 3
        assert started - 2 <= _settled_at(db, legacy) <= time.time() + 2
        later = time.time() + RETRY_HORIZON_S + 60
        assert db._execute_write(lambda c: compact_settled_results(c, now=later)) == 1
        assert admission_result(db, legacy)['result']['messages'] == []


def test_a_pass_is_bounded_and_takes_the_oldest_first(tmp_path):
    db, epoch = _db(tmp_path)
    with db:
        ids = [_settle(db, epoch, f'r{i}')[0] for i in range(5)]
        for admission_id in ids:
            _age(db, admission_id, RETRY_HORIZON_S + 60)
        assert db._execute_write(lambda c: compact_settled_results(c, limit=2)) == 2
        emptied = [admission_result(db, i)['result']['messages'] == [] for i in ids]
        assert emptied == [True, True, False, False, False]
        assert db._execute_write(lambda c: compact_settled_results(c, limit=10)) == 3


def test_an_unsettled_admission_and_a_malformed_result_are_left_alone(tmp_path):
    db, epoch = _db(tmp_path)
    with db:
        rt.admit_session_input(db, epoch=epoch, principal_id='api', session_id='chat', request_id='running',
                               payload={'text': 'still running'})
        row = rt.claim_session_input(db, epoch=epoch, session_id='chat')
        db._execute_write(lambda c: c.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                                              (RESULT_PREFIX + row['admission_id'], json.dumps(
                                                  {'result': {'messages': [{'role': 'assistant', 'content': 'x'}]}}))))
        db._execute_write(lambda c: c.execute('UPDATE session_admissions SET settled_at=1.0'))
        assert db._execute_write(compact_settled_results) == 0  # started, not terminal
        db.create_session('other', source='api_server')  # 'chat' still has its running turn
        settled, _ = _settle(db, epoch, 'bad-row', session='other')
        _age(db, settled, RETRY_HORIZON_S + 60)
        for value in ('not json', '[1, 2]', '{"result": "flat"}', '{"result": {"messages": "text"}}'):
            db._execute_write(lambda c, value=value: c.execute('UPDATE state_meta SET value=? WHERE key=?',
                                                               (value, RESULT_PREFIX + settled)))
            assert db._execute_write(compact_settled_results) == 0, value  # terminal and aged, but not a turn result
            assert _size(db, settled) == len(value)


def test_a_turn_that_ended_without_a_final_reply_keeps_its_messages(tmp_path):
    db, epoch = _db(tmp_path)
    with db:
        silent, _ = _settle(db, epoch, 'silent', reply='')
        _age(db, silent, RETRY_HORIZON_S + 60)
        stored = admission_result(db, silent)
        assert db._execute_write(compact_settled_results) == 0
        assert admission_result(db, silent) == stored and len(stored['result']['messages']) == 3


def test_a_row_inserted_already_terminal_is_stamped(tmp_path):
    db, _ = _db(tmp_path)
    with db:
        db._execute_write(lambda c: c.execute(
            "INSERT INTO session_admissions(admission_id,request_id,principal_id,target_session_id,lineage_json,"
            "payload_json,payload_digest,intent,status,outcome) VALUES('imported','r','api','chat','[]','{}','d',"
            "'queue','terminal','completed')"))
        assert abs(_settled_at(db, 'imported') - time.time()) < 5


def test_the_prune_pass_runs_it(tmp_path):
    db, epoch = _db(tmp_path)
    with db:
        old, _ = _settle(db, epoch, 'old')
        _age(db, old, RETRY_HORIZON_S + 60)
        db.prune_sessions(older_than_days=90)
        assert admission_result(db, old)['result']['messages'] == []
        assert db.get_session('chat') is not None
