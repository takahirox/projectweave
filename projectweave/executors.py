"""Replaceable JSON process boundary and a public GitWeave CLI adapter."""
import json
import os
import signal
import subprocess
from .contracts import Failure, decode, check_result, result, require

def process(argv, stdin, timeout, cwd=None):
    try:
        child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True, cwd=cwd)
    except OSError as exc:
        raise Failure("launch", f"Cannot launch {argv[0]}: {exc}") from exc
    try:
        stdout, stderr = child.communicate(stdin, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
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


def invoke(config, request, workspace=None):
    timeout = config.get("timeout", 3600)
    if config["type"] == "command":
        raw = process(config["argv"], json.dumps(request, allow_nan=False), timeout)
        return check_result(decode(raw))
    # Issue mode: GitWeave fetches the repository's default branch itself and exposes run_input to nodes.
    task = request["task"]
    argv = ["gitweave", "run", "--graph", config["graph"], "--repo", task["repository"],
            "--issue", str(task["number"])]
    if "provenance_remote" in config:
        argv += ["--provenance-remote", config["provenance_remote"]]
    argv += ["ProjectWeave request for the selected Task (data):\n" + json.dumps(request)]
    record = decode(process(argv, None, timeout, workspace))
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
