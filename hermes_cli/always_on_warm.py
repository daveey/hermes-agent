"""Joined, deadline-owned warm rounds for the stdlib-only roller."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


def warm(roller, port, paths, deadline, *, workers, request_timeout):
    summary = {"paths": len(paths)}

    def request(path):
        left = deadline - roller.clock()
        if left <= 0:
            return None
        return roller.get(port, path, min(request_timeout, left))

    # No detached work: even exceptions leave after all admitted transport calls close.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for round_no in (1, 2):
            if roller.clock() >= deadline:
                summary[f"round{round_no}"] = "skipped (cap)"
                break
            pending = set()
            remaining = iter(paths)
            results = []
            exhausted = False
            while True:
                while len(pending) < workers and not exhausted and roller.clock() < deadline:
                    path = next(remaining, None)
                    if path is None:
                        exhausted = True
                        break
                    pending.add(pool.submit(request, path))
                if not pending:
                    break
                done, pending = wait(pending, timeout=max(0, deadline - roller.clock()), return_when=FIRST_COMPLETED)
                for future in done:
                    try:
                        result = future.result()
                    except Exception:  # health: allow BLE001 -- warm transport errors are best effort, pool still joined
                        result = None
                    if result is not None:
                        results.append(result)
                if roller.clock() >= deadline:
                    break
            times = sorted(elapsed for _status, elapsed, _body in results)
            summary[f"round{round_no}"] = {"done": len(results), "max_s": round(times[-1], 2) if times else None,
                                          "errors": sum(1 for status, *_ in results if status != 200)}
    return summary
