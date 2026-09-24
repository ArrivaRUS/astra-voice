#!/usr/bin/env bash
set -uo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
OUT="$HERE/out"
SERVER='/home/astra/Документы/Astra Cowork/astra-cowork/.venv/bin/python'
CLIENT='/home/astra/.cache/astra-voice-dev/venv-a/bin/python'
mkdir -p "$OUT"

if [[ ${1:-} != --inner ]]; then
    unset DBUS_SESSION_BUS_ADDRESS S9_ISOLATED
    {
        echo "S9 DATE 2026-09-24 HOST $(hostname) KERNEL $(uname -srmo)"
        env -u DISPLAY -u WAYLAND_DISPLAY QT_QPA_PLATFORM=offscreen PULSE_SERVER=unix:/nonexistent \
            "$SERVER" -B -c 'import sys,PyQt6.QtCore; print("S9 SERVER",sys.executable,sys.version.split()[0],PyQt6.QtCore.PYQT_VERSION_STR,PyQt6.QtCore.QT_VERSION_STR)'
        env -u DISPLAY -u WAYLAND_DISPLAY QT_QPA_PLATFORM=offscreen PULSE_SERVER=unix:/nonexistent \
            "$CLIENT" -B -c 'import sys,PyQt5.QtCore; print("S9 CLIENT",sys.executable,sys.version.split()[0],PyQt5.QtCore.PYQT_VERSION_STR,PyQt5.QtCore.QT_VERSION_STR)'
        env -u DISPLAY -u WAYLAND_DISPLAY QT_QPA_PLATFORM=offscreen PULSE_SERVER=unix:/nonexistent \
            timeout 230s dbus-run-session -- bash "$HERE/run.sh" --inner
    } 2>&1 | tee "$OUT/run.log"
    rc=${PIPESTATUS[0]}
    if ! grep -q '^SUMMARY ' "$OUT/run.log"; then
        echo "SUMMARY NOT_RUN rc=$rc (private D-Bus unavailable)" | tee -a "$OUT/run.log"
    fi
    exit "$rc"
fi

address=${DBUS_SESSION_BUS_ADDRESS:-}
if [[ $address != unix:* || $address == "unix:path=/run/user/$(id -u)/bus" ]]; then
    echo "S9 guard: private unix D-Bus session required; address=$address"
    exit 2
fi
export S9_ISOLATED=1
export QT_QPA_PLATFORM=offscreen PULSE_SERVER=unix:/nonexistent
unset DISPLAY WAYLAND_DISPLAY
fail=0
server_pid=
monitor_pid=
cleanup() {
    if [[ -n $monitor_pid ]]; then kill "$monitor_pid" 2>/dev/null || :; wait "$monitor_pid" 2>/dev/null || :; monitor_pid=; fi
    if [[ -n $server_pid ]]; then kill "$server_pid" 2>/dev/null || :; wait "$server_pid" 2>/dev/null || :; server_pid=; fi
}
trap cleanup EXIT

start_server() {
    local name=$1; shift
    "$SERVER" -B "$HERE/server_pyqt6.py" "$@" >"$OUT/$name.server.stdout" 2>"$OUT/$name.server.log" &
    server_pid=$!
    local n
    for ((n=0; n<100; n++)); do
        if grep -qx READY "$OUT/$name.server.stdout" 2>/dev/null; then
            echo "SERVER $name READY pid=$server_pid flags=$*"
            return 0
        fi
        if ! kill -0 "$server_pid" 2>/dev/null; then break; fi
        sleep 0.02
    done
    echo "SERVER $name FAILED"
    cat "$OUT/$name.server.log"
    fail=$((fail+1))
    cleanup
    return 1
}

stop_server() {
    if [[ -n $server_pid ]]; then kill "$server_pid" 2>/dev/null || :; wait "$server_pid" 2>/dev/null || :; server_pid=; fi
}

run_client() {
    local name=$1
    "$CLIENT" -B "$HERE/client_pyqt5.py" "$name"
    local rc=$?
    if ((rc != 0)); then fail=$((fail+1)); fi
    return 0
}

count_check() {
    local scenario=$1 expected=$2 pattern=$3 file=$4
    local actual
    actual=$(grep -c "$pattern" "$file" 2>/dev/null || :)
    if [[ $actual == "$expected" ]]; then
        echo "SCENARIO $scenario EXPECTED $expected RESULT $actual PASS"
    else
        echo "SCENARIO $scenario EXPECTED $expected RESULT $actual FAIL"
        fail=$((fail+1))
    fi
}

echo "S9 DATE 2026-09-24 HOST $(hostname) UID $(id -u) ADDRESS $address"
echo "S9 SERVER $("$SERVER" -B -c 'import sys,PyQt6.QtCore; print(sys.version.split()[0],PyQt6.QtCore.PYQT_VERSION_STR,PyQt6.QtCore.QT_VERSION_STR)')"
echo "S9 CLIENT $("$CLIENT" -B -c 'import sys,PyQt5.QtCore; print(sys.version.split()[0],PyQt5.QtCore.PYQT_VERSION_STR,PyQt5.QtCore.QT_VERSION_STR)')"
# Scenario 2a: this private bus returned NameHasNoOwner with auto-start disabled.
# Both NameHasNoOwner and ServiceUnknown prove that the service is not running (V1).
run_client 2a
start_server basic --no-limits --executor instant --echo-ints
run_client 1a
run_client 1b
# The monitor belongs only to this private bus. It records wire signatures for 1c.
dbus-monitor --session "type='method_call',interface='ru.astralinux.Cowork.Command1'" "type='method_return'" >"$OUT/wire.log" 2>"$OUT/wire.err" &
monitor_pid=$!
run_client 1c
kill "$monitor_pid" 2>/dev/null || :; wait "$monitor_pid" 2>/dev/null || :; monitor_pid=
awk '
    function report() {
        if (scenario != "") {
            print "WIRE " scenario " sent_mono_ms=" sent " deadline_mono_ms=" deadline
            seen[scenario] = 1
        }
    }
    /^(method call|method return|signal) / {
        report()
        scenario = ""
        field = ""
        sent = "not_observed"
        deadline = "not_observed"
    }
    /string "probe_plain_big"/ { scenario = "1c_plain_big" }
    /string "probe_i_big"/ { scenario = "1c_i_big" }
    /string "sent_mono_ms"/ { field = "sent"; next }
    /string "deadline_mono_ms"/ { field = "deadline"; next }
    field != "" && $1 == "variant" {
        if (field == "sent") sent = $2 " " $3
        else deadline = $2 " " $3
        field = ""
    }
    END {
        report()
        if (!("1c_plain_big" in seen)) print "WIRE 1c_plain_big not_observed"
        if (!("1c_i_big" in seen)) print "WIRE 1c_i_big not_observed"
    }
' "$OUT/wire.log"
count_check 1c_plain_big_wire 1 'string "probe_plain_big"' "$OUT/wire.log"
run_client 1d
run_client 2b
run_client 2c
stop_server
start_server bad --bad-reply
run_client 2d
stop_server
start_server slow --block-before-ms 600
run_client 2e
run_client 4a
sleep 0.4
count_check 4a_server_expired 2 'REPLY expired deadline' "$OUT/slow.server.log"
stop_server
start_server dedup --executor never
run_client 3a
count_check 3a_work 1 'WORK_START' "$OUT/dedup.server.log"
stop_server
start_server conflict --executor never
run_client 3b
stop_server
start_server rate --executor never
run_client 3c
stop_server
start_server queue --executor never
run_client 3d
count_check 3d_work 3 'WORK_START' "$OUT/queue.server.log"
stop_server
start_server quit --quit-after-reply --executor never
run_client 3e_quit
for ((n=0; n<100; n++)); do
    if ! kill -0 "$server_pid" 2>/dev/null; then break; fi
    sleep 0.02
done
stop_server
echo "SCENARIO 3e_quit_work RESULT $(grep -c 'WORK_START' "$OUT/quit.server.log" || :) starts (observed)"
start_server guard --shutdown-after-reply --executor never
run_client 3e_guard
for ((n=0; n<100; n++)); do
    if ! kill -0 "$server_pid" 2>/dev/null; then break; fi
    sleep 0.02
done
stop_server
count_check 3e_guard_work 0 'WORK_START' "$OUT/guard.server.log"
count_check 3e_guard_skipped 1 'WORK_SKIPPED_SHUTDOWN' "$OUT/guard.server.log"
start_server exit --exit-after-reply --executor never
run_client 3e_exit
for ((n=0; n<100; n++)); do
    if ! kill -0 "$server_pid" 2>/dev/null; then break; fi
    sleep 0.02
done
stop_server
count_check 3e_exit_work 0 'WORK_START' "$OUT/exit.server.log"
start_server post --block-after-reply-ms 600 --executor instant
run_client 4b
sleep 0.4
stop_server
start_server measure --no-limits --executor instant
run_client 5
stop_server
"$SERVER" -B -c 'import pathlib,sys; a=sorted(float(x.split()[1]) for x in pathlib.Path(sys.argv[1]).read_text().splitlines() if x.startswith("UID_LOOKUP ")); p=lambda q: a[round((len(a)-1)*q/100)] if a else None; print("MEASURE GetConnectionUnixUser", "n",len(a),"p50_ms",p(50),"p95_ms",p(95))' "$OUT/measure.server.log"
if ((fail == 0)); then echo 'SUMMARY PASS rc=0'; else echo "SUMMARY FAIL count=$fail rc=1"; fi
exit "$((fail != 0))"
