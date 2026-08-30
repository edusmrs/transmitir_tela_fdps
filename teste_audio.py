"""
Script de diagnóstico de áudio
================================
Roda alguns testes pra descobrir por que o áudio não está sendo
capturado/transmitido, sem precisar rodar o app inteiro.

Uso:
    python testar_audio.py
"""

import sys

try:
    import soundcard as sc
except ImportError:
    print("ERRO: a biblioteca 'soundcard' não está instalada.")
    print("Rode: pip install soundcard")
    sys.exit(1)

import numpy as np

print(f"Sistema operacional detectado: {sys.platform}\n")

print("=== Alto-falantes (speakers) disponíveis ===")
speakers = sc.all_speakers()
for s in speakers:
    print(f"  - {s.name}")

print("\n=== Microfones / dispositivos de entrada disponíveis ===")
mics = sc.all_microphones(include_loopback=True)
for m in mics:
    tipo = "LOOPBACK" if m.isloopback else "microfone normal"
    print(f"  - {m.name}  [{tipo}]")

print("\n=== Alto-falante padrão ===")
try:
    default_speaker = sc.default_speaker()
    print(f"  {default_speaker.name}")
except Exception as e:
    print(f"  ERRO ao obter alto-falante padrão: {e}")
    sys.exit(1)

print("\n=== Tentando abrir o loopback do alto-falante padrão ===")
try:
    mic = sc.get_microphone(id=str(default_speaker.name), include_loopback=True)
    print(f"  Dispositivo de loopback encontrado: {mic.name}")
except Exception as e:
    print(f"  ERRO: não foi possível obter o microfone de loopback: {e}")
    print("\n  DICA: no Linux, isso geralmente precisa do PulseAudio ou PipeWire")
    print("  com o módulo 'module-loopback' ou 'pipewire-pulse' ativo.")
    sys.exit(1)

print("\n=== Gravando 3 segundos de áudio (toque alguma música/som agora) ===")
try:
    with mic.recorder(samplerate=48000, channels=2) as recorder:
        data = recorder.record(numframes=48000 * 3)
    print(f"  Gravação concluída. Formato: {data.shape}, tipo: {data.dtype}")

    volume = np.abs(data).mean()
    print(f"  Volume médio capturado: {volume:.6f}")
    if volume < 0.0001:
        print("  AVISO: o volume capturado está em praticamente zero.")
        print("  Isso normalmente significa que nenhum som estava tocando,")
        print("  ou que o dispositivo de loopback errado foi selecionado.")
    else:
        print("  Áudio capturado com sucesso! O loopback está funcionando.")
except Exception as e:
    print(f"  ERRO durante a gravação: {e}")
    sys.exit(1)

print("\n=== Testando reprodução (tocando de volta os 3 segundos gravados) ===")
try:
    with default_speaker.player(samplerate=48000, channels=2) as player:
        player.play(data)
    print("  Reprodução concluída. Se você ouviu o som de volta, está tudo OK.")
except Exception as e:
    print(f"  ERRO durante a reprodução: {e}")