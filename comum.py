import collections
import contextlib
import math
import os
import queue
import random
import socket
import sys
import threading
import time
import unicodedata

EH_WINDOWS = os.name == "nt"
if EH_WINDOWS:
    import msvcrt
else:
    import select
    import termios
    import tty


PORTA_PADRAO = 5000
TEMPO_PREPARO = 10
TEMPO_TURNO = 30
MAX_TENTATIVAS = 3
PONTOS_DEFESA = 5
PONTOS_POR_TENTATIVA = {1: 10, 2: 7, 3: 5}
PAUSA_ENTRE_RODADAS = 5
TAM_MIN, TAM_MAX = 3, 12

MODOS = {
    0: {"nome": "Clássico", "bonus": False,
        "desc": "pontos pela tentativa + bônus da dificuldade da regra"},
    1: {"nome": "Desafio", "bonus": True,
        "desc": "tudo do Clássico + bônus de velocidade + bônus de combo"},
}

DEBUG = False


def configurar_debug(argumentos):
    global DEBUG
    DEBUG = "--debug" in argumentos
    return [a for a in argumentos if a != "--debug"]


SEP = ":"
Mensagem = collections.namedtuple("Mensagem", "vez cod campos bruta")


def montar(vez, cod, *campos):
    texto = f"{vez}{cod}"
    if campos:
        texto += SEP + SEP.join(str(c) for c in campos)
    return texto


def interpretar(linha):
    if len(linha) < 2 or linha[0] not in "012" or not linha[1].isalpha():
        return None
    campos = linha[3:].split(SEP) if len(linha) > 2 and linha[2] == SEP else []
    return Mensagem(int(linha[0]), linha[1].upper(), campos, linha)


def campo_int(campos, i, padrao=0):
    try:
        return int(campos[i])
    except (IndexError, ValueError):
        return padrao


def campo_float(campos, i, padrao=0.0):
    try:
        return float(campos[i])
    except (IndexError, ValueError):
        return padrao


def campo_str(campos, i, padrao=""):
    return campos[i] if i < len(campos) else padrao


class ConexaoPerdida(Exception):
    pass


class AdversarioSaiu(ConexaoPerdida):
    pass


class MemoriaCompartilhada:

    def __init__(self, meu_id):
        self.trava = threading.Lock()
        self.chegou = threading.Condition(self.trava)
        self.meu_id = meu_id
        self.vez = 2
        self.rodada = 1
        self.modo = 0
        self.placar = {1: 0, 2: 0}
        self.sequencia = {1: 0, 2: 0}
        self.nomes = {1: "Jogador 1", 2: "Jogador 2"}
        self.fila = collections.deque()
        self.conectado = True
        self.saiu = False
        self.tempo_restante = 0.0

    def ler_vez(self):
        with self.trava:
            return self.vez

    def trocar_vez(self):
        with self.trava:
            self.vez = 3 - self.vez
            self.rodada += 1
            return self.vez

    def foto(self):
        with self.trava:
            return {"vez": self.vez, "rodada": self.rodada, "modo": self.modo,
                    "placar": dict(self.placar), "nomes": dict(self.nomes)}

    def definir_nomes(self, nome1, nome2):
        with self.trava:
            self.nomes = {1: nome1, 2: nome2}

    def definir_modo(self, modo):
        with self.trava:
            self.modo = modo

    def definir_tempo(self, segundos):
        with self.trava:
            self.tempo_restante = segundos

    def ler_tempo(self):
        with self.trava:
            return self.tempo_restante

    def sequencia_de(self, jogador):
        with self.trava:
            return self.sequencia[jogador]

    def aplicar_resultado(self, decifrador, acertou, ganhador, pontos):
        with self.trava:
            self.placar[ganhador] += pontos
            self.sequencia[decifrador] = self.sequencia[decifrador] + 1 if acertou else 0
            return self.placar[1], self.placar[2]

    def sincronizar_resultado(self, decifrador, acertou, p1, p2):
        with self.trava:
            self.placar = {1: p1, 2: p2}
            self.sequencia[decifrador] = self.sequencia[decifrador] + 1 if acertou else 0

    def depositar(self, msg):
        with self.chegou:
            self.fila.append(msg)
            self.chegou.notify_all()

    def retirar(self, timeout=None):
        fim = None if timeout is None else time.monotonic() + timeout
        with self.chegou:
            while not self.fila:
                if not self.conectado:
                    raise AdversarioSaiu() if self.saiu else ConexaoPerdida()
                espera = 0.5
                if fim is not None:
                    espera = fim - time.monotonic()
                    if espera <= 0:
                        return None
                self.chegou.wait(min(espera, 0.5))
            return self.fila.popleft()

    def marcar_desconexao(self, saiu=False):
        with self.chegou:
            self.conectado = False
            self.saiu = self.saiu or saiu
            self.chegou.notify_all()

    def esta_conectado(self):
        with self.trava:
            return self.conectado

    def verificar_conexao(self):
        with self.trava:
            if not self.conectado and not self.fila:
                raise AdversarioSaiu() if self.saiu else ConexaoPerdida()


class Conexao:
    def __init__(self, sock, mem):
        self.sock = sock
        self.mem = mem
        self.trava_envio = threading.Lock()
        self.receptora = threading.Thread(target=self._receber_sempre, daemon=True)

    def iniciar(self):
        self.receptora.start()

    def enviar(self, vez, cod, *campos):
        texto = montar(vez, cod, *campos)
        if DEBUG:
            print(f"   [rede >>] {texto}")
        with self.trava_envio:
            try:
                self.sock.sendall((texto + "\n").encode("utf-8"))
            except OSError:
                self.mem.marcar_desconexao()
                raise ConexaoPerdida()

    def _receber_sempre(self):
        buffer = b""
        try:
            while True:
                dados = self.sock.recv(1024)
                if not dados:
                    break
                buffer += dados
                if len(buffer) > 8192 and b"\n" not in buffer:
                    buffer = b""
                while b"\n" in buffer:
                    linha, buffer = buffer.split(b"\n", 1)
                    texto = linha.decode("utf-8", errors="replace").strip()
                    msg = interpretar(texto) if texto else None
                    if msg is None:
                        continue
                    if msg.vez == 0 and msg.cod == "S":
                        self.mem.marcar_desconexao(saiu=True)
                        return
                    self.mem.depositar(msg)
        except OSError:
            pass
        finally:
            self.mem.marcar_desconexao()

    def fechar(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def receber(con, mem, codigos=None, timeout=None):
    fim = None if timeout is None else time.monotonic() + timeout
    while True:
        restante = None if fim is None else max(0.0, fim - time.monotonic())
        msg = mem.retirar(restante)
        if msg is None:
            return None
        if DEBUG:
            print(f"   [rede <<] {msg.bruta}")
        if msg.vez in (1, 2) and msg.vez != mem.ler_vez():
            if DEBUG:
                print("   [rede] mensagem fora de vez descartada")
            continue
        if codigos and msg.cod not in codigos:
            if DEBUG:
                print("   [rede] código inesperado descartado")
            continue
        return msg


class Cronometro(threading.Thread):
    def __init__(self, mem, duracao):
        super().__init__(daemon=True)
        self.mem = mem
        self.fim = time.monotonic() + duracao
        self._parar = threading.Event()
        mem.definir_tempo(duracao)

    def run(self):
        while not self._parar.is_set():
            restante = max(0.0, self.fim - time.monotonic())
            self.mem.definir_tempo(restante)
            if restante <= 0:
                break
            self._parar.wait(0.1)

    def parar(self):
        self._parar.set()


def limpar_tela():
    os.system("cls" if EH_WINDOWS else "clear")


_TERMINAL = sys.stdin.isatty()
_fila_linhas = None


def _iniciar_leitor_linhas():
    global _fila_linhas
    if _fila_linhas is None:
        _fila_linhas = queue.Queue()

        def ler():
            for linha in sys.stdin:
                _fila_linhas.put(linha.rstrip("\r\n"))
            _fila_linhas.put(None)

        threading.Thread(target=ler, daemon=True).start()


def entrada(prompt):
    if _TERMINAL:
        return input(prompt)
    print(prompt, end="", flush=True)
    _iniciar_leitor_linhas()
    linha = _fila_linhas.get()
    if linha is None:
        raise EOFError
    print(linha)
    return linha


def _descartar_teclas_pendentes():
    if not _TERMINAL:
        return
    if EH_WINDOWS:
        while msvcrt.kbhit():
            msvcrt.getwch()
    else:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)


@contextlib.contextmanager
def _modo_tecla_a_tecla():
    if EH_WINDOWS:
        yield
        return
    fd = sys.stdin.fileno()
    antigo = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, antigo)


def _ler_tecla(timeout):
    if EH_WINDOWS:
        fim = time.monotonic() + timeout
        while time.monotonic() < fim:
            if msvcrt.kbhit():
                c = msvcrt.getwch()
                if c in ("\x00", "\xe0"):
                    msvcrt.getwch()
                    continue
                return c
            time.sleep(0.02)
        return None
    pronto, _, _ = select.select([sys.stdin], [], [], timeout)
    if pronto:
        return os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")
    return None


def ler_com_tempo(rotulo, prazo_tentativa, mem):
    _descartar_teclas_pendentes()

    if not _TERMINAL:
        _iniciar_leitor_linhas()
        print(f"[restam {mem.ler_tempo():.0f}s] {rotulo} > ", end="", flush=True)
        while True:
            if not mem.esta_conectado():
                print()
                return "", "desconexao"
            if mem.ler_tempo() <= 0:
                print()
                return "", "tempo_total"
            if time.monotonic() >= prazo_tentativa:
                print()
                return "", "tempo_tentativa"
            try:
                linha = _fila_linhas.get(timeout=0.1)
            except queue.Empty:
                continue
            if linha is None:
                raise EOFError
            print(linha)
            return linha, "ok"

    buffer = ""
    ultimo = ""
    with _modo_tecla_a_tecla():
        while True:
            if not mem.esta_conectado():
                print()
                return buffer, "desconexao"
            total = mem.ler_tempo()
            if total <= 0:
                print()
                return buffer, "tempo_total"
            if time.monotonic() >= prazo_tentativa:
                print()
                return buffer, "tempo_tentativa"

            tent = max(0, math.ceil(prazo_tentativa - time.monotonic()))
            texto = f"[total {math.ceil(total):2d}s | tentativa {tent:2d}s] {rotulo} > {buffer}"
            if texto != ultimo:
                sys.stdout.write("\r" + texto.ljust(len(ultimo)) + "\r" + texto)
                sys.stdout.flush()
                ultimo = texto

            tecla = _ler_tecla(0.1)
            if tecla is None or tecla == "":
                continue
            if tecla in ("\r", "\n"):
                print()
                return buffer, "ok"
            if tecla in ("\x08", "\x7f"):
                buffer = buffer[:-1]
            elif tecla == "\x03":
                raise KeyboardInterrupt
            elif tecla.isprintable() and len(buffer) < 40:
                buffer += tecla


def contagem_regressiva(mem, segundos, mensagem, final="VALENDO!"):
    for s in range(segundos, 0, -1):
        mem.verificar_conexao()
        sys.stdout.write(f"\r{mensagem}... {s:2d}s ")
        sys.stdout.flush()
        time.sleep(1)
    print(f"\r{mensagem}... {final}   ")


def cabecalho(mem, titulo):
    f = mem.foto()
    modo = MODOS[f["modo"]]["nome"]
    n, p = f["nomes"], f["placar"]
    limpar_tela()
    print("=" * 64)
    print("          QUAL A REGRA DO JOGO? / QUAL CRIPTOGRAFIA?")
    print("                   ~ Duelo de Criptografia ~")
    print("=" * 64)
    print(f" Rodada {f['rodada']}  |  Modo: {modo}  |  vez = {f['vez']} "
          f"({n[f['vez']]} decifra)")
    print(f" Placar: {n[1]} {p[1]}  x  {p[2]} {n[2]}")
    print("-" * 64)
    print(f" {titulo}")
    print("-" * 64)


def pedir_nome(padrao):
    nome = entrada(f"Seu nome (ENTER = {padrao}): ").strip()
    nome = nome.replace(SEP, "").replace("\n", "")[:15]
    return nome or padrao


def escolher_modo():
    print("\nModos de jogo:")
    for k, m in MODOS.items():
        print(f"  {k} - {m['nome']:<9} {m['desc']}")
    while True:
        r = entrada("Escolha o modo (ENTER = 0): ").strip() or "0"
        if r.isdigit() and int(r) in MODOS:
            return int(r)
        print(f"Entrada inválida, digite um número de 0 a {len(MODOS) - 1}.")


REGRAS = {
    "E": {"menu": 1, "nome": "Espelho", "bonus": 0,
          "tipo": "a palavra foi escrita de trás para frente"},
    "V": {"menu": 2, "nome": "Vogais por números", "bonus": 0,
          "tipo": "as vogais foram trocadas por números"},
    "C": {"menu": 3, "nome": "Cifra de César", "bonus": 3,
          "tipo": "cada letra foi deslocada algumas posições no alfabeto"},
    "K": {"menu": 4, "nome": "Combo (Espelho + César)", "bonus": 6,
          "tipo": "a palavra foi invertida E as letras foram deslocadas no alfabeto"},
}
VOGAIS = {"A": "1", "E": "2", "I": "3", "O": "4", "U": "5"}


def cesar(palavra, n):
    return "".join(chr((ord(c) - 65 + n) % 26 + 65) for c in palavra)


def cifrar(palavra, regra, n=0):
    if regra == "E":
        return palavra[::-1]
    if regra == "V":
        return "".join(VOGAIS.get(c, c) for c in palavra)
    if regra == "C":
        return cesar(palavra, n)
    if regra == "K":
        return cesar(palavra[::-1], n)
    return palavra


def normalizar(texto):
    sem_acento = unicodedata.normalize("NFD", texto.strip().upper())
    sem_acento = "".join(c for c in sem_acento if unicodedata.category(c) != "Mn")
    return sem_acento.replace(" ", "")


def so_letras(t):
    return bool(t) and all("A" <= c <= "Z" for c in t)


DICA_PALAVRA = ("  DICA: use uma palavra simples, só com letras (acentos são removidos).\n"
                "  Exemplos: CASA, GATO, REDE, SOCKET, THREAD, MEMORIA, PROCESSO")

DICA_PALPITE = ("  DICA: digite a palavra ORIGINAL, só com letras.\n"
                "  Observe a cifrada: tem números no lugar de letras? Está de trás\n"
                "  para frente? As letras parecem 'andadas' no alfabeto (CASA -> DBTB)?")


def validar_palavra(texto):
    t = normalizar(texto)
    if not so_letras(t):
        return None, ("Palavra inválida, digite apenas letras (sem números, "
                      "espaços ou símbolos).\n" + DICA_PALAVRA)
    if not TAM_MIN <= len(t) <= TAM_MAX:
        return None, (f"Palavra inválida, ela precisa ter de {TAM_MIN} a {TAM_MAX} "
                      f"letras.\n" + DICA_PALAVRA)
    return t, None


def validar_palpite(texto):
    t = normalizar(texto)
    if so_letras(t):
        return t, None
    return None, "Entrada inválida, digite a palavra usando apenas letras.\n" + DICA_PALPITE


def mascara(palavra, palpite):
    return "".join(c if i < len(palpite) and palpite[i] == c else "_"
                   for i, c in enumerate(palavra))


def espacado(texto):
    return " ".join(texto)


def explicar(palavra, cifrada, regra, n):
    if regra == "E":
        return [f"Leia de trás para frente: {cifrada} -> {palavra}"]
    if regra == "V":
        return ["Troque os números pelas vogais: 1=A 2=E 3=I 4=O 5=U",
                f"{cifrada} -> {palavra}"]
    if regra == "C":
        passos = "  ".join(f"{c}-{n}={p}" for c, p in zip(cifrada, palavra))
        return [f"Cada letra andou +{n}. Volte {n} letras no alfabeto:", passos]
    if regra == "K":
        voltou = cesar(cifrada, -n)
        return [f"1) Volte {n} letras no alfabeto: {cifrada} -> {voltou}",
                f"2) Leia de trás para frente:    {voltou} -> {palavra}"]
    return []


def calcular_pontos(mem, decifrador, tentativa, tempo_restante, regra):
    base = PONTOS_POR_TENTATIVA.get(tentativa, 5)
    breg = REGRAS.get(regra, {"bonus": 0})["bonus"]
    vel = combo = 0
    if MODOS[mem.foto()["modo"]]["bonus"]:
        vel = min(10, int(tempo_restante // 3))
        combo = 2 * mem.sequencia_de(decifrador)
    return base + breg + vel + combo, base, vel, combo, breg


def nome_regra(regra, n):
    nome = REGRAS.get(regra, {"nome": "?"})["nome"]
    return f"{nome} (+{n})" if regra in ("C", "K") else nome


def mostrar_resultado(mem, res, palavra, cifrada, regra, n, ganhador,
                      pontos, base, vel, combo, breg):
    f = mem.foto()
    nomes, decif = f["nomes"], f["vez"]
    print("\n" + "=" * 64)
    print(f"  Cifrada: {cifrada}   |   Original: {palavra}   |   Regra: {nome_regra(regra, n)}")
    if res == "A":
        print(f"  DECIFRADO! {nomes[decif]} quebrou o código.")
        det = f"{base} pela tentativa"
        for valor, txt in ((breg, "da regra"), (vel, "de velocidade"), (combo, "de combo")):
            if valor:
                det += f" + {valor} {txt}"
        print(f"  Vencedor da rodada: {nomes[ganhador]}  (+{pontos} pontos: {det})")
    else:
        motivo = "as 3 tentativas acabaram" if res == "E" else "o tempo acabou"
        print(f"  O código resistiu: {motivo}.")
        print("  Como decifrar:")
        for linha in explicar(palavra, cifrada, regra, n):
            print(f"     {linha}")
        print(f"  Vencedor da rodada: {nomes[ganhador]}  (+{pontos} pontos de defesa)")
    p = f["placar"]
    print(f"  Placar: {nomes[1]} {p[1]}  x  {p[2]} {nomes[2]}")
    print("=" * 64)


def apresentar(con, mem, meu_nome):
    if mem.meu_id == 1:
        con.enviar(0, "H", meu_nome, mem.foto()["modo"])
        msg = receber(con, mem, ("H",), timeout=60)
    else:
        msg = receber(con, mem, ("H",), timeout=60)
        if msg is not None:
            modo = campo_int(msg.campos, 1, 0)
            mem.definir_modo(modo if modo in MODOS else 0)
            con.enviar(0, "H", meu_nome)
    if msg is None:
        raise ConexaoPerdida("O outro lado não se apresentou.")
    outro = campo_str(msg.campos, 0) or f"Jogador {3 - mem.meu_id}"
    if mem.meu_id == 1:
        mem.definir_nomes(meu_nome, outro)
    else:
        mem.definir_nomes(outro, meu_nome)


def _escolher_regra(palavra):
    print("\nEscolha a regra de criptografia:")
    for cod, r in REGRAS.items():
        exemplo = cifrar(palavra, cod, 1)
        extra = f"  (+{r['bonus']} pts p/ quem decifrar)" if r["bonus"] else ""
        print(f"  {r['menu']} - {r['nome']:<24} ex.: {palavra} -> {exemplo}{extra}")
    por_menu = {r["menu"]: cod for cod, r in REGRAS.items()}
    while True:
        op = entrada("Regra (1 a 4): ").strip()
        if op.isdigit() and int(op) in por_menu:
            regra = por_menu[int(op)]
            break
        print("Entrada inválida, digite um número de 1 a 4.")

    n = 0
    if regra in ("C", "K"):
        while True:
            op = entrada("Deslocamento de 1 a 5 (ENTER = sorteado): ").strip()
            if op == "":
                n = random.randint(1, 5)
                break
            if op.isdigit() and 1 <= int(op) <= 5:
                n = int(op)
                break
            print("Entrada inválida, digite um número inteiro de 1 a 5.")
    return regra, n


def turno_cifrar(con, mem):
    vez = mem.ler_vez()
    eu = mem.meu_id
    nomes = mem.foto()["nomes"]
    cabecalho(mem, f"Você CIFRA a mensagem. {nomes[vez]} vai tentar decifrar.")

    while True:
        palavra, erro = validar_palavra(entrada(f"Palavra secreta ({TAM_MIN} a {TAM_MAX} letras): "))
        if erro:
            print(erro)
            continue
        regra, n = _escolher_regra(palavra)
        cifrada = cifrar(palavra, regra, n)
        if cifrada == palavra:
            print(f"Com essa regra, {palavra} não muda ({cifrada}). Escolha outra palavra ou regra.")
            continue
        break

    con.enviar(vez, "N", cifrada)
    print(f"\nMensagem cifrada enviada: {cifrada}   (original: {palavra}, "
          f"regra: {nome_regra(regra, n)})")
    print(f"{nomes[vez]} terá {TEMPO_PREPARO}s de preparo, depois {TEMPO_TURNO}s "
          f"e {MAX_TENTATIVAS} tentativas.\nAcompanhe as jogadas:\n")

    while True:
        msg = receber(con, mem, ("T", "X"))
        if msg.cod == "X":
            print(f"  >> O tempo de {nomes[vez]} acabou!")
            _fechar_rodada(con, mem, "X", palavra, cifrada, regra, n, eu,
                           PONTOS_DEFESA, 0, 0, 0, 0)
            return

        k = campo_int(msg.campos, 0, MAX_TENTATIVAS)
        palpite = normalizar(campo_str(msg.campos, 1))
        tempo = campo_float(msg.campos, 2)

        if palpite == palavra:
            print(f"  Tentativa {k}: {palpite}  -> DECIFROU! (restavam {tempo:.0f}s)")
            pts, base, vel, combo, breg = calcular_pontos(mem, vez, k, tempo, regra)
            _fechar_rodada(con, mem, "A", palavra, cifrada, regra, n, vez,
                           pts, base, vel, combo, breg)
            return

        if palpite:
            print(f"  Tentativa {k}: {palpite} -> errou (restam {tempo:.0f}s)")
        else:
            print(f"  Tentativa {k}: (sem resposta, o tempo da tentativa acabou)")

        if k >= MAX_TENTATIVAS:
            _fechar_rodada(con, mem, "E", palavra, cifrada, regra, n, eu,
                           PONTOS_DEFESA, 0, 0, 0, 0)
            return
        if k == 1:
            con.enviar(vez, "D", 1, regra)
        else:
            con.enviar(vez, "D", k, regra, mascara(palavra, palpite), palavra[0])


def _fechar_rodada(con, mem, res, palavra, cifrada, regra, n, ganhador,
                   pts, base, vel, combo, breg):
    vez = mem.ler_vez()
    p1, p2 = mem.aplicar_resultado(vez, res == "A", ganhador, pts)
    con.enviar(vez, "R", res, palavra, regra, n, ganhador, pts, base, vel, combo, breg, p1, p2)
    mostrar_resultado(mem, res, palavra, cifrada, regra, n, ganhador,
                      pts, base, vel, combo, breg)
    mem.trocar_vez()


def _ler_palpite(mem, k):
    faltam = MAX_TENTATIVAS - k + 1
    limite = mem.ler_tempo() / faltam
    prazo = time.monotonic() + limite
    print(f"\nTentativa {k}/{MAX_TENTATIVAS}  (limite desta tentativa: {limite:.0f}s)")
    while True:
        texto, motivo = ler_com_tempo(f"Palavra (T{k})", prazo, mem)
        if motivo == "desconexao":
            mem.verificar_conexao()
            raise ConexaoPerdida()
        if motivo == "tempo_total" or (motivo == "tempo_tentativa" and k == MAX_TENTATIVAS):
            return "tempo", ""
        if motivo == "tempo_tentativa":
            print("  O tempo desta tentativa acabou! Tentativa perdida.")
            return "vazia", ""
        palpite, erro = validar_palpite(texto)
        if erro:
            print(erro)
            continue
        return "ok", palpite


def turno_decifrar(con, mem):
    eu = mem.meu_id
    nomes = mem.foto()["nomes"]
    adv = 3 - eu
    cabecalho(mem, f"Você DECIFRA. Aguardando {nomes[adv]} cifrar uma palavra...")

    msg = receber(con, mem, ("N",))
    cifrada = campo_str(msg.campos, 0, "?")
    print(f"\n  >>> Mensagem interceptada de {nomes[adv]}:   {espacado(cifrada)}")
    print("      Descubra a regra e digite a palavra ORIGINAL! <<<\n")
    contagem_regressiva(mem, TEMPO_PREPARO, "Prepare-se, sua vez de jogar está chegando")

    crono = Cronometro(mem, TEMPO_TURNO)
    crono.start()
    resultado = None
    try:
        for k in range(1, MAX_TENTATIVAS + 1):
            print(f"  Cifrada: {espacado(cifrada)}")
            situacao, palpite = _ler_palpite(mem, k)
            if situacao == "tempo":
                print("\n  TEMPO ESGOTADO!")
                con.enviar(eu, "X", k)
                break
            con.enviar(eu, "T", k, palpite, f"{mem.ler_tempo():.1f}")
            resposta = receber(con, mem, ("D", "R"), timeout=20)
            if resposta is None:
                raise ConexaoPerdida("O adversário parou de responder.")
            if resposta.cod == "R":
                resultado = resposta
                break
            if palpite:
                print(f"  Errou! {palpite} não é a palavra.")
            regra = campo_str(resposta.campos, 1)
            tipo = REGRAS.get(regra, {"tipo": "?"})["tipo"]
            print(f"  Dica 1: {tipo}.")
            if campo_int(resposta.campos, 0) >= 2:
                masc = campo_str(resposta.campos, 2)
                print(f"  Dica 2: letras certas na posição certa:  {espacado(masc)}")
                print(f"          a palavra começa com {campo_str(resposta.campos, 3)} "
                      f"e tem {len(masc)} letras.")
    finally:
        crono.parar()

    if resultado is None:
        resultado = receber(con, mem, ("R",), timeout=20)
        if resultado is None:
            raise ConexaoPerdida("O adversário parou de responder.")

    c = resultado.campos
    res = campo_str(c, 0, "E")
    mem.sincronizar_resultado(eu, res == "A", campo_int(c, 10), campo_int(c, 11))
    mostrar_resultado(mem, res, campo_str(c, 1), cifrada, campo_str(c, 2), campo_int(c, 3),
                      campo_int(c, 4, adv), campo_int(c, 5), campo_int(c, 6),
                      campo_int(c, 7), campo_int(c, 8), campo_int(c, 9))
    mem.trocar_vez()


def perguntar_continuar(con, mem):
    while True:
        r = entrada("\nJogar mais uma rodada dupla? (S/N): ").strip().upper()
        if r in ("S", "N"):
            break
        print("Entrada inválida, digite S ou N.")
    con.enviar(0, "C", r)
    if r == "S":
        print("Aguardando a resposta do adversário...")
    msg = receber(con, mem, ("C",))
    if campo_str(msg.campos, 0) != "S" and r == "S":
        print("O adversário preferiu encerrar.")
    return r == "S" and campo_str(msg.campos, 0) == "S"


def tela_final(mem, motivo):
    f = mem.foto()
    n, p = f["nomes"], f["placar"]
    print("\n" + "#" * 64)
    print(f"  {motivo}")
    print(f"  PLACAR FINAL: {n[1]} {p[1]}  x  {p[2]} {n[2]}")
    if p[1] == p[2]:
        print("  Resultado: EMPATE!")
    else:
        print(f"  Campeão: {n[1] if p[1] > p[2] else n[2]}")
    print("#" * 64)


def jogar(con, mem):
    try:
        continuar = True
        while continuar:
            for i in range(2):
                if mem.ler_vez() == mem.meu_id:
                    turno_decifrar(con, mem)
                else:
                    turno_cifrar(con, mem)
                if i == 0:
                    contagem_regressiva(mem, PAUSA_ENTRE_RODADAS,
                                        "Papéis invertidos! Próxima rodada em",
                                        final="agora!")
            continuar = perguntar_continuar(con, mem)
        motivo = "Fim de jogo! Obrigado por jogar."
    except AdversarioSaiu:
        motivo = "O adversário saiu do jogo."
    except ConexaoPerdida as e:
        motivo = f"Conexão perdida com o adversário. {e}".strip()
    except (KeyboardInterrupt, EOFError):
        motivo = "Você saiu do jogo."
        try:
            con.enviar(0, "S")
        except ConexaoPerdida:
            pass
    finally:
        con.fechar()
    tela_final(mem, motivo)
