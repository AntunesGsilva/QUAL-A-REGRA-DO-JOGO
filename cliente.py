import socket
import sys

# Jogador 2
# "python cliente.py 127.0.0.1 --debug", adicionado isso ao terminal para começar o jogo com debug

from comum import (PORTA_PADRAO, Conexao, ConexaoPerdida, MemoriaCompartilhada,
                   apresentar, configurar_debug, entrada, jogar, pedir_nome)


def conectar(ip, porta):
    while True:
        try:
            print(f"Conectando em {ip}:{porta}...")
            sock = socket.create_connection((ip, porta), timeout=5)
            sock.settimeout(None)
            return sock
        except OSError as e:
            print(f"Não foi possível conectar: {e}")
            print("O servidor (python servidor.py) já está aberto e esperando?")
            if entrada("Tentar de novo? (S/N): ").strip().upper() != "S":
                return None


def main():
    args = configurar_debug(sys.argv[1:])
    ip = args[0] if args else None
    porta = int(args[1]) if len(args) > 1 and args[1].isdigit() else PORTA_PADRAO

    print("=" * 62)
    print("        QUAL A REGRA DO JOGO? - CLIENTE (Jogador 2)")
    print("=" * 62)
    if ip is None:
        ip = entrada("IP do servidor (ENTER = 127.0.0.1): ").strip() or "127.0.0.1"
    nome = pedir_nome("Jogador 2")

    sock = conectar(ip, porta)
    if sock is None:
        return
    print("Conectado!")

    mem = MemoriaCompartilhada(meu_id=2)
    con = Conexao(sock, mem)
    con.iniciar()

    try:
        apresentar(con, mem, nome)
    except (ConexaoPerdida, KeyboardInterrupt) as e:
        print(f"\nFalha na apresentação: {e}")
        con.fechar()
        return
    jogar(con, mem)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nEncerrado.")
