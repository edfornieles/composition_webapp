"""Persistent MLX model server.

Run as:  python _mlx_server.py <base_model> [<adapter_dir>]

Protocol: newline-delimited JSON on stdin/stdout.
  First output line: {"status": "ready"} or {"status": "error", "error": "..."}
  Each request line:  {"prompt": "...", "max_tokens": N, "temperature": T,
                       "top_p": P, "repetition_penalty": R, "stop_sequences": [...]}
  Each response line: {"text": "...", "error": null, "debug": {...}}
"""
import json
import os
import sys


def _trim_at_stops(text, stops):
    earliest, matched = -1, ""
    for stop in stops:
        if not stop:
            continue
        i = text.find(stop)
        if i != -1 and (earliest == -1 or i < earliest):
            earliest, matched = i, stop
    if earliest == -1:
        return text, ""
    return text[:earliest].rstrip(), matched


def main():
    base_model = sys.argv[1]
    adapter_dir = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None

    try:
        from mlx_lm import load, generate  # type: ignore
    except Exception as e:
        print(json.dumps({"status": "error", "error": f"mlx_lm_import_failed:{e}"}), flush=True)
        sys.exit(1)

    try:
        kwargs = {"adapter_path": adapter_dir} if adapter_dir and os.path.exists(adapter_dir) else {}
        model, tokenizer = load(base_model, **kwargs)
    except Exception as e:
        print(json.dumps({"status": "error", "error": f"mlx_load_failed:{e}"}), flush=True)
        sys.exit(1)

    print(json.dumps({"status": "ready"}), flush=True)

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            req = json.loads(raw_line)
        except Exception as e:
            print(json.dumps({"text": "", "error": f"bad_request_json:{e}", "debug": {}}), flush=True)
            continue

        try:
            prompt = req.get("prompt") or ""
            max_tokens = int(req.get("max_tokens") or 200)
            temperature = float(req.get("temperature") or 0.9)
            top_p = float(req.get("top_p") or 0.95)
            rep_penalty = float(req.get("repetition_penalty") or 1.0)
            stop_sequences = req.get("stop_sequences") or []

            logits_processors = None
            rep_processor_used = False
            if rep_penalty and rep_penalty != 1.0:
                try:
                    from mlx_lm.sample_utils import make_logits_processors  # type: ignore
                    logits_processors = make_logits_processors(repetition_penalty=rep_penalty)
                    rep_processor_used = True
                except Exception:
                    try:
                        from mlx_lm.sample_utils import make_repetition_penalty  # type: ignore
                        logits_processors = [make_repetition_penalty(rep_penalty)]
                        rep_processor_used = True
                    except Exception:
                        logits_processors = None

            sampler = None
            try:
                from mlx_lm.sample_utils import make_sampler  # type: ignore
                sampler = make_sampler(temp=temperature, top_p=top_p)
            except Exception:
                sampler = None

            gen_kwargs = {"max_tokens": max_tokens, "verbose": False}
            if sampler is not None:
                gen_kwargs["sampler"] = sampler
            if logits_processors is not None:
                gen_kwargs["logits_processors"] = logits_processors
            elif rep_penalty and rep_penalty != 1.0:
                gen_kwargs["repetition_penalty"] = rep_penalty

            out = generate(model, tokenizer, prompt=prompt, **gen_kwargs)
            text = str(out).strip()
            text, matched_stop = _trim_at_stops(text, stop_sequences)

            print(json.dumps({
                "text": text,
                "error": None,
                "debug": {
                    "rep_processor_used": rep_processor_used,
                    "stop_matched": matched_stop,
                },
            }), flush=True)
        except Exception as e:
            print(json.dumps({"text": "", "error": f"mlx_generate_failed:{e}", "debug": {}}), flush=True)


if __name__ == "__main__":
    main()
