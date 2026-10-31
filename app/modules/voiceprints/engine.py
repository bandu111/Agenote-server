"""Real ECAPA inference, VAD and conservative quality gates; no synthetic identity."""
import io
import threading
import wave
from pathlib import Path
import numpy as np


class SampleRejected(ValueError):
    def __init__(self, reason, *, speech_ms=None, window_ms=None):
        super().__init__(reason)
        self.speech_ms, self.window_ms = speech_ms, window_ms


def decode_wav(raw):
    try:
        with wave.open(io.BytesIO(raw), 'rb') as stream:
            if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getcomptype()) != (1, 2, 16000, 'NONE'):
                raise ValueError()
            frames = stream.getnframes()
            pcm = stream.readframes(frames)
            if len(pcm) != frames * 2 or not 8 * 16000 <= frames <= 20 * 16000:
                raise ValueError()
        return pcm
    except (wave.Error, EOFError, ValueError) as error:
        raise SampleRejected('invalid_audio') from error


def unit(value):
    value = np.asarray(value, dtype=np.float32)
    norm = np.linalg.norm(value)
    if not np.isfinite(value).all() or norm < 1e-8:
        raise SampleRejected('invalid_embedding')
    return value / norm


class VoiceprintEngine:
    def __init__(self, settings):
        self.settings, self.model = settings, None
        self.lock = threading.Lock()

    @property
    def ready(self):
        root = Path(self.settings.voiceprint_model_path)
        return all((root / name).is_file() for name in ('hyperparams.yaml', 'embedding_model.ckpt', 'mean_var_norm_emb.ckpt', 'classifier.ckpt'))

    def load(self):
        if not self.ready:
            raise RuntimeError('voiceprint_model_unavailable')
        if self.model is None:
            import torch
            from speechbrain.inference.speaker import SpeakerRecognition
            torch.set_num_threads(2)
            # Local model only: user speech never leaves this backend for inference.
            root = str(Path(self.settings.voiceprint_model_path).resolve())
            self.model = SpeakerRecognition.from_hparams(source=root, savedir=root,
                overrides={'pretrained_path': root}, run_opts={'device': 'cpu'})

    def inspect(self, raw):
        return self._inspect_pcm(decode_wav(raw), min_speech=6, wait_for_model=True)

    def prewarm(self):
        """只加载权重并用零张量预热算子；不生成用户模板或匹配证据。"""
        with self.lock:
            self.load()
            import torch
            with torch.inference_mode():
                self.model.encode_batch(torch.zeros(1, 3 * 16000))

    def inspect_live(self, pcm):
        """实时短片段复用注册的特征处理，忙时跳过，不在线程池排队等模型。"""
        if len(pcm) % 2 or not 3 * 32000 <= len(pcm) <= 5 * 32000:
            raise SampleRejected('insufficient_speech')
        return self._inspect_pcm(pcm, min_speech=3, wait_for_model=False)

    def _inspect_pcm(self, pcm, *, min_speech, wait_for_model):
        import webrtcvad
        values = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
        clipping = float(np.mean(np.abs(values) >= .999))
        if clipping > .01:
            raise SampleRejected('clipping')
        vad = webrtcvad.Vad(2)
        frame_size = 480  # 30 ms at 16 kHz.
        frames = [values[i:i + frame_size] for i in range(0, len(values) - frame_size + 1, frame_size)]
        voiced = [vad.is_speech(pcm[i * 960:(i + 1) * 960], 16000) for i in range(len(frames))]
        speech_seconds = sum(voiced) * .03
        if speech_seconds < min_speech or sum(voiced) / len(voiced) < .45:
            raise SampleRejected('insufficient_speech')
        speech = np.concatenate([f for f, yes in zip(frames, voiced) if yes])
        if float(np.sqrt(np.mean(speech ** 2))) < .008:
            raise SampleRejected('too_quiet')
        quiet = [f for f, yes in zip(frames, voiced) if not yes]
        if quiet:
            noise = np.concatenate(quiet)
            snr = 10 * np.log10((np.mean(speech ** 2) + 1e-10) / (np.mean(noise ** 2) + 1e-10))
            if snr < 8:
                raise SampleRejected('too_noisy')
        if not self.lock.acquire(blocking=wait_for_model):
            raise SampleRejected('engine_busy')
        try:
            self.load()
            import torch
            # Compare several windows to reject obvious mixed speakers. This is
            # not a dedicated overlap detector and cannot guarantee single-speaker audio.
            windows = np.array_split(speech, 3)
            with torch.inference_mode():
                vectors = [unit(self.model.encode_batch(torch.from_numpy(w).unsqueeze(0)).reshape(-1).cpu().numpy()) for w in windows]
            minimum = min(float(np.dot(vectors[i], vectors[j])) for i in range(3) for j in range(i))
            if minimum < self.settings.voiceprint_consistency_threshold:
                # 短窗拒绝率需实测评估；仅附带时长，不暴露特征或相似度。
                raise SampleRejected('inconsistent_voice', speech_ms=round(speech_seconds * 1000),
                                     window_ms=round(min(map(len, windows)) / 16))
            embedding = unit(np.mean(vectors, axis=0)).tolist()
        finally:
            self.lock.release()
        return embedding, {'speechSeconds': round(speech_seconds, 2), 'clippedFraction': clipping,
                           'qualityRuleVersion': 'vad-window-v1', 'overlapDetection': 'window_consistency_only'}
