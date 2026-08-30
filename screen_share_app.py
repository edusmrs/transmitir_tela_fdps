"""
App de Transmissão de Tela - Interface Gráfica
================================================
Requisitos:
    pip install mss opencv-python numpy requests soundcard

Como usar:
    python screen_share_app.py

Escolha "Servidor" para transmitir sua tela, ou "Cliente" para
assistir a tela de outra pessoa. Informe IP e porta e clique em Iniciar.
"""

import socket
import struct
import pickle
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox

import cv2
import numpy as np
import mss

try:
    import requests
except ImportError:
    requests = None

try:
    import soundcard as sc
except ImportError:
    sc = None

AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 2
AUDIO_BLOCK_SIZE = 2048    # blocos maiores = menos estalos, um pouco mais de latência
AUDIO_QUEUE_MAX = 8        # limite de blocos em espera antes de descartar os mais antigos
                            # (evita que o atraso do áudio cresça sem parar)

RESOLUTIONS = {
    "Original (tela cheia)": None,
    "1920x1080": (1920, 1080),
    "1600x900": (1600, 900),
    "1280x720 (recomendado)": (1280, 720),
    "960x540": (960, 540),
}


class ScreenShareApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Transmissão de Tela")
        self.root.geometry("440x460")
        self.root.resizable(False, False)

        self.running = False
        self.sock = None
        self.audio_sock = None
        self.conn = None
        self.thread = None

        # Suporte a múltiplos espectadores (modo servidor)
        self.clients = []          # sockets de vídeo conectados
        self.clients_lock = threading.Lock()
        self.accept_thread = None

        self.audio_clients = []    # sockets de áudio conectados
        self.audio_clients_lock = threading.Lock()
        self.audio_accept_thread = None
        self.audio_send_thread = None      # thread que captura o áudio (produtor)
        self.audio_broadcast_thread = None  # thread que envia pela rede (consumidor)
        self.audio_capture_queue = None    # fila entre captura e envio (lado servidor)

        self.audio_recv_thread = None      # thread que recebe da rede (produtor)
        self.audio_play_thread = None      # thread que toca o áudio (consumidor)
        self.audio_playback_queue = None   # fila entre rede e reprodução (lado cliente)

        self.mode_var = tk.StringVar(value="server")
        self.ip_var = tk.StringVar(value="0.0.0.0")
        self.port_var = tk.StringVar(value="27015")
        self.audio_port_var = tk.StringVar(value="27016")
        self.quality_var = tk.IntVar(value=60)
        self.resolution_var = tk.StringVar(value="1280x720 (recomendado)")
        self.audio_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="Parado")

        self._build_ui()

    # ---------------------------------------------------------- UI ----
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        title = ttk.Label(
            self.root, text="Transmissão de Tela entre Amigos",
            font=("Segoe UI", 13, "bold")
        )
        title.pack(pady=(15, 10))

        # Modo (Servidor / Cliente)
        mode_frame = ttk.LabelFrame(self.root, text="Modo")
        mode_frame.pack(fill="x", **pad)

        ttk.Radiobutton(
            mode_frame, text="Servidor (transmitir minha tela)",
            variable=self.mode_var, value="server",
            command=self._on_mode_change
        ).pack(anchor="w", padx=10, pady=3)

        ttk.Radiobutton(
            mode_frame, text="Cliente (assistir tela de um amigo)",
            variable=self.mode_var, value="client",
            command=self._on_mode_change
        ).pack(anchor="w", padx=10, pady=3)

        # IP e Porta
        conn_frame = ttk.LabelFrame(self.root, text="Conexão")
        conn_frame.pack(fill="x", **pad)

        ip_row = ttk.Frame(conn_frame)
        ip_row.pack(fill="x", padx=10, pady=5)
        self.ip_label = ttk.Label(ip_row, text="IP (0.0.0.0 = escutar em todas):", width=28)
        self.ip_label.pack(side="left")
        ttk.Entry(ip_row, textvariable=self.ip_var).pack(side="left", fill="x", expand=True)

        port_row = ttk.Frame(conn_frame)
        port_row.pack(fill="x", padx=10, pady=5)
        ttk.Label(port_row, text="Porta (vídeo):", width=28).pack(side="left")
        ttk.Entry(port_row, textvariable=self.port_var).pack(side="left", fill="x", expand=True)

        audio_port_row = ttk.Frame(conn_frame)
        audio_port_row.pack(fill="x", padx=10, pady=5)
        ttk.Label(audio_port_row, text="Porta (áudio):", width=28).pack(side="left")
        ttk.Entry(audio_port_row, textvariable=self.audio_port_var).pack(side="left", fill="x", expand=True)

        res_row = ttk.Frame(conn_frame)
        res_row.pack(fill="x", padx=10, pady=5)
        ttk.Label(res_row, text="Resolução (lado servidor):", width=28).pack(side="left")
        ttk.Combobox(
            res_row, textvariable=self.resolution_var,
            values=list(RESOLUTIONS.keys()), state="readonly"
        ).pack(side="left", fill="x", expand=True)

        quality_row = ttk.Frame(conn_frame)
        quality_row.pack(fill="x", padx=10, pady=5)
        ttk.Label(quality_row, text="Qualidade JPEG (10-100):", width=28).pack(side="left")
        ttk.Entry(quality_row, textvariable=self.quality_var).pack(side="left", fill="x", expand=True)

        audio_row = ttk.Frame(conn_frame)
        audio_row.pack(fill="x", padx=10, pady=5)
        self.audio_check = ttk.Checkbutton(
            audio_row, text="Transmitir/ouvir áudio do sistema",
            variable=self.audio_var
        )
        self.audio_check.pack(side="left")

        # Botão para descobrir IP público (útil no modo servidor)
        self.ip_btn = ttk.Button(
            conn_frame, text="Descobrir meu IP público",
            command=self._show_public_ip
        )
        self.ip_btn.pack(padx=10, pady=(0, 8), anchor="w")

        # Botões de ação
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(pady=10)

        self.start_btn = ttk.Button(btn_frame, text="Iniciar", command=self.start)
        self.start_btn.pack(side="left", padx=5)

        self.stop_btn = ttk.Button(btn_frame, text="Parar", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=5)

        # Status
        status_frame = ttk.Frame(self.root)
        status_frame.pack(fill="x", padx=10, pady=(10, 0))
        ttk.Label(status_frame, text="Status:").pack(side="left")
        ttk.Label(status_frame, textvariable=self.status_var, foreground="blue").pack(side="left", padx=5)

        note = ttk.Label(
            self.root,
            text="Dica: na janela de vídeo do cliente, arraste as bordas\n"
                 "ou clique em maximizar — a imagem acompanha o tamanho.",
            foreground="gray", justify="center"
        )
        note.pack(pady=(15, 0))

        if sc is None:
            self.audio_check.config(state="disabled")
            self.audio_var.set(False)
            warn = ttk.Label(
                self.root, text="(instale 'soundcard' para habilitar áudio: pip install soundcard)",
                foreground="red"
            )
            warn.pack()

        self._on_mode_change()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_mode_change(self):
        if self.mode_var.get() == "server":
            self.ip_label.config(text="IP (0.0.0.0 = escutar em todas):")
            self.ip_var.set("0.0.0.0")
        else:
            self.ip_label.config(text="IP do servidor (do seu amigo):")
            self.ip_var.set("")

    def _show_public_ip(self):
        if requests is None:
            messagebox.showwarning("Aviso", "Instale a biblioteca 'requests' para usar isso:\npip install requests")
            return

        def fetch():
            try:
                ip = requests.get("https://api.ipify.org", timeout=5).text
                messagebox.showinfo("Seu IP público", f"Seu IP público é:\n{ip}\n\nPasse esse IP para quem for assistir.")
            except Exception as e:
                messagebox.showerror("Erro", f"Não foi possível obter o IP público:\n{e}")

        threading.Thread(target=fetch, daemon=True).start()

    # ------------------------------------------------------- Controle ----
    def start(self):
        if self.running:
            return

        port_str = self.port_var.get().strip()
        audio_port_str = self.audio_port_var.get().strip()
        ip_str = self.ip_var.get().strip()

        if not port_str.isdigit():
            messagebox.showerror("Erro", "Porta de vídeo inválida.")
            return
        port = int(port_str)

        use_audio = self.audio_var.get() and sc is not None
        audio_port = None
        if use_audio:
            if not audio_port_str.isdigit():
                messagebox.showerror("Erro", "Porta de áudio inválida.")
                return
            audio_port = int(audio_port_str)
            if audio_port == port:
                messagebox.showerror("Erro", "A porta de áudio precisa ser diferente da porta de vídeo.")
                return

        mode = self.mode_var.get()
        if mode == "client" and not ip_str:
            messagebox.showerror("Erro", "Informe o IP do servidor.")
            return

        self.running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")

        if mode == "server":
            self.status_var.set("Aguardando conexão...")
            self.thread = threading.Thread(
                target=self._run_server, args=(ip_str, port, audio_port), daemon=True
            )
        else:
            self.status_var.set("Conectando...")
            self.thread = threading.Thread(
                target=self._run_client, args=(ip_str, port, audio_port), daemon=True
            )

        self.thread.start()

    def stop(self):
        self.running = False
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass

        with self.clients_lock:
            for c in self.clients:
                try:
                    c.close()
                except Exception:
                    pass
            self.clients.clear()

        with self.audio_clients_lock:
            for c in self.audio_clients:
                try:
                    c.close()
                except Exception:
                    pass
            self.audio_clients.clear()

        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        try:
            if self.audio_sock:
                self.audio_sock.close()
        except Exception:
            pass
        cv2.destroyAllWindows()
        self.status_var.set("Parado")
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

    def _on_close(self):
        self.stop()
        self.root.destroy()

    # --------------------------------------------------------- Servidor ----
    def _run_server(self, ip, port, audio_port):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.bind((ip, port))
            self.sock.listen(5)  # permite várias conexões na fila

            self.status_var.set("Aguardando espectadores...")

            # Thread separada só para aceitar novas conexões de vídeo
            self.accept_thread = threading.Thread(
                target=self._accept_clients, daemon=True
            )
            self.accept_thread.start()

            # Áudio: socket dedicado na porta configurada separadamente.
            # Isolado em try/except próprio: se o áudio falhar, o vídeo continua.
            use_audio = self.audio_var.get() and sc is not None and audio_port is not None
            if use_audio:
                try:
                    self.audio_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    self.audio_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    self.audio_sock.bind((ip, audio_port))
                    self.audio_sock.listen(5)

                    self.audio_capture_queue = queue.Queue(maxsize=AUDIO_QUEUE_MAX)

                    self.audio_accept_thread = threading.Thread(
                        target=self._accept_audio_clients, daemon=True
                    )
                    self.audio_accept_thread.start()

                    self.audio_send_thread = threading.Thread(
                        target=self._capture_and_broadcast_audio, daemon=True
                    )
                    self.audio_send_thread.start()

                    self.audio_broadcast_thread = threading.Thread(
                        target=self._broadcast_audio_worker, daemon=True
                    )
                    self.audio_broadcast_thread.start()
                    print(f"[áudio] Servidor de áudio escutando em {ip}:{audio_port}")
                except Exception as e:
                    print(f"[áudio] ERRO ao abrir porta de áudio {audio_port}: {e}")
                    self.status_var.set(f"Vídeo OK, mas áudio falhou na porta {audio_port}: {e}")

            quality = self.quality_var.get()
            target_res = RESOLUTIONS.get(self.resolution_var.get())

            with mss.mss() as sct:
                monitor = sct.monitors[1]
                while self.running:
                    img = np.array(sct.grab(monitor))
                    img = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

                    if target_res is not None:
                        img = cv2.resize(img, target_res)
                    # se target_res é None, mantém a resolução original da tela

                    result, encoded_img = cv2.imencode(
                        '.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, quality]
                    )
                    data = pickle.dumps(encoded_img)
                    message = struct.pack("Q", len(data)) + data

                    self._broadcast(message)

        except Exception as e:
            if self.running:
                self.status_var.set(f"Erro: {e}")
        finally:
            self.root.after(0, self.stop)

    def _accept_clients(self):
        """Fica aceitando novos espectadores de vídeo enquanto o servidor roda."""
        while self.running:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                break  # socket foi fechado (Parar foi clicado)

            if not self.running:
                try:
                    conn.close()
                except Exception:
                    pass
                break

            with self.clients_lock:
                self.clients.append(conn)

            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

            self.root.after(0, self._update_status_clients)

    def _accept_audio_clients(self):
        """Fica aceitando novas conexões de áudio."""
        while self.running:
            try:
                conn, addr = self.audio_sock.accept()
            except OSError:
                break

            if not self.running:
                try:
                    conn.close()
                except Exception:
                    pass
                break

            with self.audio_clients_lock:
                self.audio_clients.append(conn)

            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass

    def _capture_and_broadcast_audio(self):
        """Captura o áudio do sistema (loopback) e só o coloca numa fila.
        Não faz rede aqui — assim uma rede lenta nunca trava a captura,
        que é a causa mais comum de áudio robotizado/entrecortado."""
        try:
            speaker = sc.default_speaker()
            mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            print(f"[áudio] Capturando loopback de: {mic.name}")
            with mic.recorder(samplerate=AUDIO_SAMPLE_RATE, channels=AUDIO_CHANNELS) as recorder:
                while self.running:
                    data = recorder.record(numframes=AUDIO_BLOCK_SIZE)
                    data = data.astype(np.float32).tobytes()
                    self._queue_put_drop_old(self.audio_capture_queue, data)
        except Exception as e:
            # Áudio é um recurso extra; se falhar, a transmissão de vídeo continua normalmente.
            # Mas avisamos no console e no status para não falhar silenciosamente.
            print(f"[áudio] ERRO na captura: {e}")
            self.root.after(0, lambda: self.status_var.set(
                self.status_var.get() + " | Áudio falhou: " + str(e)
            ))

    def _broadcast_audio_worker(self):
        """Consome a fila de áudio capturado e envia pela rede.
        Roda numa thread separada da captura, para que uma rede lenta
        nunca atrase a gravação em si."""
        while self.running:
            try:
                data = self.audio_capture_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            message = struct.pack("Q", len(data)) + data
            self._broadcast_audio(message)

    @staticmethod
    def _queue_put_drop_old(q, item):
        """Coloca um item na fila; se estiver cheia, descarta o mais antigo
        em vez de bloquear. Isso impede que o atraso do áudio cresça sem parar
        quando a rede ou a reprodução ficam momentaneamente mais lentas."""
        try:
            q.put_nowait(item)
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(item)
            except queue.Full:
                pass

    def _broadcast(self, message):
        """Envia o mesmo frame de vídeo para todos os espectadores conectados."""
        with self.clients_lock:
            broken = []
            for c in self.clients:
                try:
                    c.sendall(message)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    broken.append(c)

            for c in broken:
                self.clients.remove(c)
                try:
                    c.close()
                except Exception:
                    pass

        if broken:
            self.root.after(0, self._update_status_clients)

    def _broadcast_audio(self, message):
        with self.audio_clients_lock:
            broken = []
            for c in self.audio_clients:
                try:
                    c.sendall(message)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    broken.append(c)
            for c in broken:
                self.audio_clients.remove(c)
                try:
                    c.close()
                except Exception:
                    pass

    def _update_status_clients(self):
        n = len(self.clients)
        if n == 0:
            self.status_var.set("Aguardando espectadores...")
        elif n == 1:
            self.status_var.set("Transmitindo para 1 espectador")
        else:
            self.status_var.set(f"Transmitindo para {n} espectadores")

    # ----------------------------------------------------------- Cliente ----
    def _run_client(self, ip, port, audio_port):
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10)
            self.sock.connect((ip, port))
            self.sock.settimeout(None)
            try:
                self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            self.status_var.set(f"Conectado a {ip}:{port}")

            # Tenta também conectar no áudio (porta separada). Se falhar, segue só com vídeo.
            use_audio = self.audio_var.get() and sc is not None and audio_port is not None
            if use_audio:
                self.audio_recv_thread = threading.Thread(
                    target=self._receive_and_play_audio, args=(ip, audio_port), daemon=True
                )
                self.audio_recv_thread.start()

            window_name = 'Tela compartilhada (pressione Q para sair)'
            cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
            cv2.resizeWindow(window_name, 1280, 720)

            data_buffer = b""
            payload_size = struct.calcsize("Q")

            while self.running:
                while len(data_buffer) < payload_size:
                    packet = self.sock.recv(4096)
                    if not packet:
                        raise ConnectionError("Servidor desconectou.")
                    data_buffer += packet

                packed_msg_size = data_buffer[:payload_size]
                data_buffer = data_buffer[payload_size:]
                msg_size = struct.unpack("Q", packed_msg_size)[0]

                while len(data_buffer) < msg_size:
                    packet = self.sock.recv(4096)
                    if not packet:
                        raise ConnectionError("Servidor desconectou.")
                    data_buffer += packet

                frame_data = data_buffer[:msg_size]
                data_buffer = data_buffer[msg_size:]

                encoded_img = pickle.loads(frame_data)
                frame = cv2.imdecode(encoded_img, cv2.IMREAD_COLOR)

                cv2.imshow(window_name, frame)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break

        except Exception as e:
            if self.running:
                self.status_var.set(f"Erro: {e}")
        finally:
            self.root.after(0, self.stop)

    def _receive_and_play_audio(self, ip, audio_port):
        """Conecta no socket de áudio do servidor e enfileira os blocos recebidos.
        A reprodução acontece numa thread separada (_play_audio_worker), para que
        uma rede instável nunca trave a reprodução em si."""
        audio_conn = None
        try:
            audio_conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            audio_conn.settimeout(10)
            audio_conn.connect((ip, audio_port))
            audio_conn.settimeout(None)
            try:
                audio_conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            print(f"[áudio] Conectado ao servidor de áudio em {ip}:{audio_port}")

            self.audio_playback_queue = queue.Queue(maxsize=AUDIO_QUEUE_MAX)
            self.audio_play_thread = threading.Thread(
                target=self._play_audio_worker, daemon=True
            )
            self.audio_play_thread.start()

            data_buffer = b""
            payload_size = struct.calcsize("Q")

            while self.running:
                while len(data_buffer) < payload_size:
                    packet = audio_conn.recv(65536)
                    if not packet:
                        return
                    data_buffer += packet

                packed_msg_size = data_buffer[:payload_size]
                data_buffer = data_buffer[payload_size:]
                msg_size = struct.unpack("Q", packed_msg_size)[0]

                while len(data_buffer) < msg_size:
                    packet = audio_conn.recv(65536)
                    if not packet:
                        return
                    data_buffer += packet

                chunk = data_buffer[:msg_size]
                data_buffer = data_buffer[msg_size:]

                self._queue_put_drop_old(self.audio_playback_queue, chunk)
        except Exception as e:
            # Se o áudio falhar, o vídeo continua funcionando normalmente.
            print(f"[áudio] ERRO na conexão: {e}")
            self.root.after(0, lambda: self.status_var.set(
                self.status_var.get() + " | Áudio falhou: " + str(e)
            ))
        finally:
            try:
                if audio_conn:
                    audio_conn.close()
            except Exception:
                pass

    def _play_audio_worker(self):
        """Consome a fila de áudio recebido pela rede e toca no alto-falante."""
        try:
            speaker = sc.default_speaker()
            with speaker.player(samplerate=AUDIO_SAMPLE_RATE, channels=AUDIO_CHANNELS) as player:
                while self.running:
                    try:
                        chunk = self.audio_playback_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    samples = np.frombuffer(chunk, dtype=np.float32).reshape(-1, AUDIO_CHANNELS)
                    player.play(samples)
        except Exception as e:
            print(f"[áudio] ERRO na reprodução: {e}")


def main():
    root = tk.Tk()
    style = ttk.Style()
    try:
        style.theme_use("clam")
    except Exception:
        pass
    app = ScreenShareApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()