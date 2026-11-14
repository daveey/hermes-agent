"""Quoted-subscript classification stays linear and scoped to its simple command."""

import shutil
import subprocess

import pytest

import tools.shell_arith_sink as arith_sink
from cron.lifecycle_guard import scan_gateway_lifecycle
from tools.shell_command_position import QuotedNameClassifier, quoted_name_is_arithmetic
from tools.shell_heredoc import output_reaches_shell

PRODUCER = "$(echo 'b[$(bash ./big.sh)]')"


@pytest.fixture
def prefix_lengths(monkeypatch):
    """Lengths of the source spans the classifier tokenizes, one per tokenization."""
    lengths = []
    real = QuotedNameClassifier._tokenize

    def counting(self, command, start):
        lengths.append(start - self._resume)
        return real(self, command, start)

    monkeypatch.setattr(QuotedNameClassifier, "_tokenize", counting)
    return lengths


@pytest.mark.parametrize("script", [
    "".join(f'echo "[deploy] step {i}: $(hostname)"\n' for i in range(1500)),
    "bash deploy.sh <<'EOF'\n" + "".join(
        f'echo "[INFO] line {i}: $(hostname)"\n' for i in range(1000)) + "EOF\n",
    "".join(f'git commit -m "fix [{i}]"; curl -d "[$(cat f{i})]"\n' for i in range(1000)),
])
def test_unnamed_quoted_brackets_are_never_classified(prefix_lengths, script):
    assert not arith_sink.substitution_in_arithmetic(script, date_exempt=True)
    assert prefix_lengths == []


def test_classification_reads_each_simple_command_once(prefix_lengths):
    lines = [f'printf -v "a[{i}]" %s "x[0] y[1] z[$(date +%s)]"' for i in range(1500)]
    script = "\n".join(lines) + "; " + "; ".join(f'jq "x{i}[0] y[1]"' for i in range(1500))
    assert not arith_sink.substitution_in_arithmetic(script, date_exempt=True)
    # One call per quoted word that can name a subscript, each from its own command start.
    assert len(prefix_lengths) == 3 * 1500
    assert sum(prefix_lengths) < 2 * len(script)


def test_python_program_word_is_classified_once(prefix_lengths):
    body = "\n".join(f"x{i} = d['k{i}'][0] + v[{i}]" for i in range(400))
    command = f'python3 -c "{body}"'
    arith_sink.substitution_in_arithmetic(command, date_exempt=True)
    assert len(prefix_lengths) <= 1


# One simple command with thousands of quoted subscript-like words (review 5906053691):
# each tokenization resumes at the previous word, so the total stays linear.
ONE_COMMAND = [
    "echo " + " ".join(f'"a[{i}]"' for i in range(2000)),
    "git add " + " ".join(f'"src/m{i}/f[{i}].py"' for i in range(2000)),
    "printf %s " + " ".join(f'"a[{i}]" "b"[{i}]' for i in range(1000)),
    "a=1 " * 100 + "echo " + " ".join(f'"a[{i}]"' for i in range(2000)),
    'echo "' + "".join(f'a[{i}]"x"' for i in range(2000)) + '"',
    'echo "' + "".join(f'a[{i}]$(n)' for i in range(2000)) + '"',
    "a=(" + " ".join(f'"x{i}[{i}]"' for i in range(2000)) + ")",
]


@pytest.mark.parametrize("command", ONE_COMMAND)
def test_one_command_with_many_quoted_words_tokenizes_linearly(prefix_lengths, command):
    arith_sink.substitution_in_arithmetic(command, date_exempt=True)
    assert sum(prefix_lengths) <= 2 * len(command)


@pytest.mark.parametrize("command", ONE_COMMAND[:2])
def test_one_command_with_many_quoted_words_scans_fast(tmp_path, command):
    import time
    began = time.perf_counter()
    scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert time.perf_counter() - began < 5  # ~0.2 s linear; minutes when quadratic


@pytest.mark.parametrize("seed", range(40))
def test_incremental_classification_matches_whole_prefix(seed):
    import random
    rng = random.Random(seed)
    pool = ['unset', 'printf', '-v', 'echo', 'test', 'command', 'a=1', 'if', 'time',
            '"a[1]"', '"b"[2]', "'q'\"c[3]\"", '2>"e[4]"', '>|', '"f[5]"x"g[6]"',
            'x\\\n"d[7]"', '$(echo "h[8]")', '"i[9]$(n m)j[0]"', '"k[1]" "l[2]"', '#c']
    command = " ".join(rng.choice(pool) for _ in range(rng.randint(1, 12)))
    classifier = QuotedNameClassifier(0)
    quote = ""
    for at, ch in enumerate(command):
        if ch == '"' and command[at - 1:at] != "\\":
            quote = "" if quote else '"'
        if ch != "[" or not quote or command[at - 1] in " \"'":
            continue
        assert classifier.classify(command, at) == quoted_name_is_arithmetic(command[:at]), (
            command, at)


def test_quoted_compound_array_element_is_data():
    assert not arith_sink.substitution_in_arithmetic('arr=("x[$(cat f)]")', date_exempt=True)
    assert not arith_sink.substitution_in_arithmetic(
        'declare -a arr=("[$(cat f)]=1" "y")', date_exempt=True)
    assert arith_sink.substitution_in_arithmetic('arr=([$(cat f)]=1)', date_exempt=True)
    assert arith_sink.substitution_in_arithmetic('declare -a "x=([$(cat f)]=1)"', date_exempt=True)


SINKS = [
    'echo hi; unset "a[{p}]"',
    'echo hi && unset "a[{p}]"',
    'false || unset "a[{p}]"',
    'echo hi | unset "a[{p}]"',
    'echo hi & unset "a[{p}]"; wait',
    'echo hi\nunset "a[{p}]"',
    "cat <<EOF\nbody\nEOF\nunset \"a[{p}]\"",
    "cat <<'EOF'\nbody\nEOF\nprintf -v \"a[{p}]\" %s 1",
    'v=a; unset "$v[{p}]"',
    'unset $(echo a)"[{p}]"',
    'set -- a; unset $@"[{p}]"',
    'unset "a""[{p}]"',
    "unset 'a'\"[{p}]\"",
    'declare -a "x=([{p}]=1)"',
]
DATA = [
    'unset x; echo "a[{p}]"',
    'unset x && grep "a[{p}]" /dev/null',
    'echo 2>&1 "a[{p}]"',
    'echo >| /dev/null "a[{p}]"',
    'unset "a [{p}]"',
    'declare "x=[{p}]"',
    'unset "a.[{p}]"',
    'unset "a:[{p}]"',
]


def _run(tmp_path, command):
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is required for recursive index execution proof")
    payload = tmp_path / "big.sh"
    payload.write_text("# padding\n" * 140000 + "printf ran > marker\n", encoding="utf-8")
    subprocess.run(
        [bash, "--noprofile", "--norc"], input="a=(1)\n" + command + "\n", text=True,
        encoding="utf-8", capture_output=True, timeout=20, check=False,
        cwd=tmp_path, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)},
    )
    return (tmp_path / "marker").exists()


@pytest.mark.parametrize("shape", SINKS)
def test_separated_and_concatenated_subscripts_stay_sinks(tmp_path, shape):
    command = shape.format(p=PRODUCER)
    bash = shutil.which("bash") or "bash"
    if "printf -v" not in shape or "version 3." not in subprocess.run(
            [bash, "--version"], capture_output=True, text=True, timeout=10,
            check=False).stdout:
        assert _run(tmp_path, command)
    assert output_reaches_shell(command)
    unsafe, refusal = scan_gateway_lifecycle(command, cwd=str(tmp_path))
    assert unsafe and refusal and "larger than the scan cap" in refusal


@pytest.mark.parametrize("shape", DATA)
def test_non_subscript_words_stay_data(tmp_path, shape):
    command = shape.format(p=PRODUCER)
    assert not _run(tmp_path, command)
    assert not arith_sink.substitution_in_arithmetic(
        shape.format(p="$(value)"), date_exempt=True)
