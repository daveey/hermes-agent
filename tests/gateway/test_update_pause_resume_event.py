"""A checkpoint-resumed event keeps trusted automation traits but never its internal authority."""
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource
from gateway.session_display import admission_display
from gateway.session_update_pause_ingress import pause_scope, resume_event
from gateway.warning_notifications import diagnostic_wake_muted

ROW = {'admission_id': 'adm-1', 'payload': {}, 'update_resume': {'physical_session_id': 'sid'}}


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id='c', chat_type='dm', user_id='u')


def _automation(*, heartbeat=None, diagnostic=False):
    event = MessageEvent(text='tick', source=_source(), internal=True, message_id='plat-9',
                         metadata={'notification_category': 'diagnostic'} if diagnostic else {})
    if heartbeat:
        event._heartbeat_session_id = heartbeat
    return event


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr('gateway.warning_notifications.warning_notifications_enabled',
                        lambda platform, user_config=None: False)


def test_heartbeat_owner_survives_resume():
    resumed = resume_event(ROW, _automation(heartbeat='sess-owner'))
    assert getattr(resumed, '_heartbeat_session_id', None) == 'sess-owner'


def test_diagnostic_mute_survives_resume(quiet):
    original = _automation(diagnostic=True)
    assert diagnostic_wake_muted(original) is True
    assert diagnostic_wake_muted(resume_event(ROW, original)) is True


def test_non_diagnostic_stays_unmuted(quiet):
    assert diagnostic_wake_muted(resume_event(ROW, _automation())) is False


def test_resumed_event_is_not_internal_and_has_no_control_authority():
    original = _automation(heartbeat='s')
    original.allow_gateway_control = True
    resumed = resume_event(ROW, original)
    assert resumed.internal is False and resumed.allow_gateway_control is False
    assert resumed.metadata == {}
    assert resumed.source is original.source  # provenance-bearing source is carried unchanged


def test_continuation_id_is_admission_bound_never_platform_id():
    resumed = resume_event(ROW, _automation())
    assert resumed.message_id == 'update-resume:adm-1'
    assert resume_event({**ROW, 'admission_id': 'adm-2'}, _automation()).message_id == 'update-resume:adm-2'


def test_non_resume_event_untouched():
    event = _automation()
    assert resume_event({'admission_id': 'a'}, event) is event


def test_auto_continue_display_does_not_bleed_to_none_id_followup():
    resumed = resume_event(ROW, _automation())
    followup = MessageEvent(text='later', source=_source())  # message_id None
    sibling = MessageEvent(text='x', source=_source(), message_id='plat-9')
    live = SimpleNamespace(source=_source())
    from unittest.mock import patch
    with patch('gateway.session_tui_ops._profile_home', lambda a: '/tmp'), \
            pause_scope(None, ROW, resumed, live, is_api=False):
        assert admission_display(resumed) == {'kind': 'auto_continue'}
        assert admission_display(followup) == {}
        assert admission_display(sibling) == {}


def _telegram_source(thread=None):
    return SessionSource(platform=Platform.TELEGRAM, chat_id='c', chat_type='group', user_id='u', thread_id=thread)


@pytest.mark.parametrize('thread', [None, 'topic-7'])
@pytest.mark.parametrize('internal', [True, False])
def test_synthetic_id_is_never_a_platform_reply_anchor(thread, internal):
    from gateway.platforms.base import _reply_anchor_for_event
    original = MessageEvent(text='t', source=_telegram_source(thread), internal=internal, message_id='plat-9')
    resumed = resume_event(ROW, original)
    assert resumed.message_id == 'update-resume:adm-1'
    assert resumed.reply_anchor_override == ''
    assert _reply_anchor_for_event(resumed) == ''


def test_diagnostic_category_survives_so_turn_context_mutes(quiet):
    from gateway.warning_notifications import diagnostic_metadata, diagnostic_turn_muted
    resumed = resume_event(ROW, _automation(diagnostic=True))
    assert resumed.internal is False and resumed.metadata == {}
    meta = diagnostic_metadata(resumed)  # what run_turn persists and TurnContext mutes from
    assert meta == {'notification_category': 'diagnostic'}
    assert diagnostic_turn_muted(meta, Platform.TELEGRAM) is True


def test_human_event_cannot_acquire_diagnostic_category(quiet):
    from gateway.warning_notifications import diagnostic_metadata
    human = MessageEvent(text='h', source=_source(), message_id='p', metadata={'notification_category': 'diagnostic'})
    assert diagnostic_metadata(resume_event(ROW, human)) == {}
    assert diagnostic_metadata(resume_event(ROW, _automation())) == {}


@pytest.mark.parametrize('automation', ['internal', 'heartbeat'])
def test_automation_resume_keeps_silence_but_human_does_not(automation):
    from gateway.response_filters import silence_allowed
    original = _automation(heartbeat='s') if automation == 'heartbeat' else _automation()
    resumed = resume_event(ROW, original)
    assert silence_allowed('auto_continue', resumed.reply_expected) is True
    human = resume_event(ROW, MessageEvent(text='h', source=_source(), message_id='p', reply_expected=True))
    assert human.reply_expected is True
    assert silence_allowed('auto_continue', human.reply_expected) is False
    unknown = resume_event(ROW, MessageEvent(text='h', source=_source(), message_id='p'))
    assert silence_allowed('auto_continue', unknown.reply_expected) is False
