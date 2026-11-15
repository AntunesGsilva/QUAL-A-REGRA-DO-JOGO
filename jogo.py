import sys

import cliente
import servidor
from comum import entrada


def escolher_papel():
    print("=" * 64)
    print("          QUAL A REGRA DO JOGO? / QUAL CRIPTOGRAFIA?")
    print("                   ~ Duelo de Criptografia ~")
    print("=" * 64)
    print("  1 - Servidor (Jogador 1) - abra este PRIMEIRO")
    print("  2 - Cliente  (Jogador 2) - conecta no servidor")
    while True:
        op = entrada("Escolha 1 ou 2: ").strip()
        if op == "1":
            return "servidor"
        if op == "2":
            return "cliente"
        print("Entrada inválida, digite 1 ou 2.")


def main():
    args = sys.argv[1:]
    if args and args[0] in ("servidor", "cliente"):
        papel = args.pop(0)
    else:
        papel = escolher_papel()
    sys.argv = [sys.argv[0]] + args
    if papel == "servidor":
        servidor.main()
    else:
        cliente.main()


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nEncerrado.")
    if getattr(sys, "frozen", False):
        try:
            input("\nPressione ENTER para fechar...")
        except (KeyboardInterrupt, EOFError):
            pass
