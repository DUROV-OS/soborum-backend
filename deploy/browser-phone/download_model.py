"""Install data-only local transcription weights at a recorded immutable revision."""
import json
from pathlib import Path
from huggingface_hub import model_info, snapshot_download

repository = 'Systran/faster-whisper-small'
revision = model_info(repository).sha
snapshot_download(repository, revision=revision, local_dir='/models/whisper-small')
Path('/models/whisper-model-version.json').write_text(json.dumps({'repository': repository, 'revision': revision}))
print(json.dumps({'whisper_model_ready': True, 'revision': revision}))
