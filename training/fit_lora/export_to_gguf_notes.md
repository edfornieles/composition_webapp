# Exporting the /fit/ LoRA adapter to GGUF (Ollama / llama.cpp)

The MLX-LM training step produces an adapter directory:

```
corpora/imageboards/fourchan_fit/models/fit_be_me_lora/
  adapter_config.json
  adapter_model.safetensors
  training_report.json
```

That adapter only loads under the **same MLX-LM base model**. To run via Ollama
or llama.cpp you need to:

1. **Fuse** the LoRA weights into the base model (still in MLX format):

   ```bash
   python -m mlx_lm.fuse \
     --model mlx-community/Qwen2.5-3B-Instruct-4bit \
     --adapter-path corpora/imageboards/fourchan_fit/models/fit_be_me_lora \
     --save-path corpora/imageboards/fourchan_fit/models/fit_be_me_fused
   ```

2. **Convert the fused MLX weights to a HuggingFace-format checkpoint** (mlx_lm
   ships a converter — check the version you have, the flag has changed across
   releases). Or, alternatively, train against a non-quantised HF base, then
   skip step 1 and load the merged adapter via PEFT directly.

3. **Convert HF → GGUF** using `llama.cpp`'s `convert_hf_to_gguf.py` script:

   ```bash
   git clone https://github.com/ggerganov/llama.cpp
   python llama.cpp/convert_hf_to_gguf.py \
     corpora/imageboards/fourchan_fit/models/fit_be_me_fused \
     --outfile corpora/imageboards/fourchan_fit/models/fit_be_me.gguf
   ```

4. **Quantise (optional, recommended for laptop inference):**

   ```bash
   ./llama.cpp/build/bin/quantize \
     corpora/imageboards/fourchan_fit/models/fit_be_me.gguf \
     corpora/imageboards/fourchan_fit/models/fit_be_me_q4.gguf \
     Q4_K_M
   ```

5. **Register with Ollama** using `Modelfile.ollama.example`:

   ```bash
   cd corpora/imageboards/fourchan_fit/models/
   ollama create fit-be-me -f /path/to/training/fit_lora/Modelfile.ollama.example
   ```

6. **Switch the runtime:**

   ```bash
   export FIT_LOCAL_RUNTIME=ollama
   export FIT_OLLAMA_MODEL=fit-be-me
   python manage.py test_fit_thought --screen 7_night_kitchen
   ```

## Caveats

- Tokenizer mismatches are the #1 cause of garbage output. Convert and serve
  with the same tokenizer files the base model came with.
- Quantising below Q4 hurts the be-me grammar more than you'd expect. Keep Q5
  or higher if RAM allows.
- Some Qwen2.5 builds have known GGUF conversion issues; check `llama.cpp`
  issue tracker before assuming your training is broken.
