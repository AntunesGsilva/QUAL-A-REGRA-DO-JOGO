import socket
import sys

# jogador 1 
#"python servidor.py --debug", adcinado isso ao terminal para começar o jogo com debug
from comum import (PORTA_PADRAO, Conexao, ConexaoPerdida, MemoriaCompartilhada,
                   apresentar, configurar_debug, escolher_modo, jogar, pedir_nome)


def ip_local():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def main():
    args = configurar_debug(sys.argv[1:])
    porta = int(args[0]) if args and args[0].isdigit() else PORTA_PADRAO

    print("=" * 62)
    print("        QUAL A REGRA DO JOGO? - SERVIDOR (Jogador 1)")
    print("=" * 62)
    nome = pedir_nome("Jogador 1")
    modo = escolher_modo()

    servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        servidor.bind(("0.0.0.0", porta))
    except OSError as e:
        print(f"\nNão foi possível usar a porta {porta}: {e}")
        print("Feche outro servidor aberto ou use outra porta: python servidor.py 5001")
        return
    servidor.listen(1)
    servidor.settimeout(1.0) 

    print(f"\nAguardando o Jogador 2 na porta {porta}...")
    print(f"  Mesmo computador:   python cliente.py 127.0.0.1 {porta}")
    print(f"  Outro computador:   python cliente.py {ip_local()} {porta}")
    print("  (Ctrl+C para cancelar)")

    try:
        while True:
            try:
                sock, endereco = servidor.accept()
                break
            except socket.timeout:
                continue
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
        servidor.close()
        return
    servidor.close()
    sock.settimeout(None)
    print(f"Jogador 2 conectado de {endereco[0]}!")

    mem = MemoriaCompartilhada(meu_id=1)
    mem.definir_modo(modo)
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
