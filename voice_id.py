import numpy as np
import kaldi_native_fbank as knf
import onnxruntime as ort
from huggingface_hub import hf_hub_download

VOICE_PROFILE_PATH = "voice_profile.npy"

# CAM++ speaker-embedding model (27MB ONNX, 192-dim, trained on ~200k
# speakers) - runs on onnxruntime directly, no torch needed.
_MODEL_PATH = hf_hub_download("welcomyou/campplus-3dspeaker-200k-onnx", "campplus_cn_en_common_200k.onnx")
_session = ort.InferenceSession(_MODEL_PATH, providers=["CPUExecutionProvider"])
_INPUT_NAME = _session.get_inputs()[0].name

def extract_speaker_embedding(samples):
    # samples: float32 mono audio in [-1, 1] at 16kHz (same format
    # transcribe() already converts mic audio into).
    opts = knf.FbankOptions()
    opts.frame_opts.samp_freq = 16000
    opts.mel_opts.num_bins = 80  # the model expects 80-bin fbank features
    fbank = knf.OnlineFbank(opts)
    fbank.accept_waveform(16000, samples.tolist())
    fbank.input_finished()
    frames = np.stack([fbank.get_frame(i) for i in range(fbank.num_frames_ready)]).astype(np.float32)
    frames -= frames.mean(axis=0, keepdims=True)  # CMVN
    embedding = _session.run(None, {_INPUT_NAME: frames[np.newaxis]})[0][0]
    return embedding / np.linalg.norm(embedding)
