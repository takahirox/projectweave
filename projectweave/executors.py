"""Replaceable JSON process boundary and a public GitWeave CLI adapter."""
import json
import codecs
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from .contracts import Failure, decode, check_result, result, require

def process(argv, stdin, timeout, cwd=None, on_output=None):
    try:
        child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True, cwd=cwd)
    except OSError as exc:
        raise Failure("launch", f"Cannot launch {argv[0]}: {exc}") from exc
    try:
        if on_output is None:
            stdout, stderr = child.communicate(stdin, timeout=timeout)
        else:
            stdout, stderr = stream_process(child, stdin, timeout, on_output)
    except subprocess.TimeoutExpired as exc:
        if on_output is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate()
        raise Failure("timeout", f"{argv[0]} exceeded {timeout} seconds") from exc
    except UnicodeError as exc:
        raise Failure("transport", "Process output is not valid text") from exc
    if child.returncode:
        raise Failure("transport", f"{argv[0]} exited {child.returncode}", {"stderr": stderr[-4000:]})
    return stdout


def stream_process(child, stdin, timeout, on_output):
    """Drain both pipes continuously; waiting and stdin delivery share the process timeout."""
    output = {"stdout": [], "stderr": ""}
    errors = []

    def read(stream, name):
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            while chunk := os.read(stream.fileno(), 4096):
                text = decoder.decode(chunk)
                if name == "stdout":
                    output[name].append(text)
                else:
                    output[name] = (output[name] + text)[-4000:]
                on_output(name, text)
            tail = decoder.decode(b"", final=True)
            if name == "stdout":
                output[name].append(tail)
            else:
                output[name] = (output[name] + tail)[-4000:]
            if tail:
                on_output(name, tail)
        except Exception as exc:
            errors.append(exc)
        finally:
            stream.close()

    def write():
        try:
            if stdin is not None:
                child.stdin.write(stdin)
                child.stdin.flush()
        except BrokenPipeError:
            pass
        except UnicodeError as exc:
            errors.append(exc)
        finally:
            child.stdin.close()

    threads = [threading.Thread(target=read, args=(child.stdout, "stdout")),
               threading.Thread(target=read, args=(child.stderr, "stderr")), threading.Thread(target=write)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout if timeout is not None else None
    try:
        child.wait(timeout=timeout)
        for thread in threads:
            thread.join(timeout=max(0, deadline - time.monotonic()) if deadline is not None else None)
        if any(thread.is_alive() for thread in threads):
            raise subprocess.TimeoutExpired(child.args, timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        raise
    finally:
        for thread in threads:
            thread.join()
    if errors:
        raise errors[0]
    stdout, stderr = "".join(output["stdout"]), output["stderr"]
    on_output("result", stdout)
    return stdout, stderr


def gitweave_progress(record, on_event):
    """Use returned identity and available local provenance, without scanning other Runs."""
    if not isinstance(record, dict):
        return
    if isinstance(record.get("run_id"), str):
        on_event({"type": "gitweave_run", "run_id": record["run_id"]})
    for output in record.get("outputs", []):
        if isinstance(output, dict) and isinstance(output.get("node_id"), str):
            on_event({"type": "node_completed", "node_id": output["node_id"]})
    repository = record.get("repository")
    run_ref, notes_ref = record.get("run_ref"), record.get("notes_ref")
    if not isinstance(repository, str) or not Path(repository).is_dir() or not isinstance(run_ref, str):
        return

    def read(*args):
        return json.loads(subprocess.run(["git", "-C", repository, *args], capture_output=True,
                                         text=True, check=True, timeout=5).stdout)

    try:
        provenance = read("show", f"{run_ref}:run.json")
        for attempt in provenance.get("attempts", []):
            if not isinstance(notes_ref, str) or not isinstance(attempt.get("commit"), str):
                continue
            note = read("notes", f"--ref={notes_ref}", "show", attempt["commit"])
            state = {"completed": "node_completed", "failed": "node_failed"}.get(note.get("status"))
            if state:
                on_event({"type": state, "node_id": note.get("node_id"), "instance_id": note.get("instance_id")})
    except (OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError):
        # Telemetry is optional. Missing/pruned provenance must not fail a Task.
        return


def output_observer(on_event, gitweave=False):
    pending = ""

    def output(stream, text):
        nonlocal pending
        if stream == "result":
            if gitweave:
                try:
                    gitweave_progress(json.loads(text), on_event)
                except (ValueError, TypeError):
                    pass
            return
        on_event({"type": stream, "text": text})
        # JSON-lines events on stderr coexist with the final JSON result on stdout.
        # Cap partial lines so ordinary output cannot turn this into a second unbounded buffer.
        if gitweave and stream == "stderr":
            pending += text
            while "\n" in pending:
                line, pending = pending.split("\n", 1)
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    kind = event.get("type") or event.get("event")
                    if kind == "run_started":
                        on_event({"type": "gitweave_run", "run_id": event.get("run_id")})
                    elif kind in ("node_started", "node_completed", "node_failed", "agent_output",
                                  "command_stdout", "command_stderr", "log"):
                        on_event(event)
            pending = pending[-16384:]
    return output


def invoke(config, request, workspace=None, on_event=None):
    timeout = config.get("timeout", 3600)
    options = {"on_output": output_observer(on_event, config["type"] == "gitweave")} if on_event else {}
    if config["type"] == "command":
        raw = process(config["argv"], json.dumps(request, allow_nan=False), timeout, cwd=workspace, **options)
        return check_result(decode(raw))
    # Issue mode: GitWeave fetches the repository's default branch itself and exposes run_input to nodes.
    task = request["task"]
    argv = ["gitweave", "run", "--graph", config["graph"], "--repo", task["repository"],
            "--issue", str(task["number"])]
    if "provenance_remote" in config:
        argv += ["--provenance-remote", config["provenance_remote"]]
    argv += ["ProjectWeave request for the selected Task (data):\n" + json.dumps(request)]
    record = decode(process(argv, None, timeout, workspace, **options))
    require(isinstance(record, dict) and record.get("status") == "completed"
            and isinstance(record.get("outputs"), list) and record["outputs"],
            "GitWeave did not return a completed Run with outputs", "executor")
    refs = []
    for output in record["outputs"]:
        require(isinstance(output, dict) and isinstance(output.get("commit"), str)
                and isinstance(output.get("message"), str) and "data" in output,
                "Invalid GitWeave terminal output", "executor")
        refs.append(output["commit"])
    require(all(isinstance(record.get(k), str) for k in ("run_id", "repository", "run_ref", "notes_ref")),
            "Missing GitWeave Run identity", "executor")
    return result("GitWeave graph completed", record, refs)
