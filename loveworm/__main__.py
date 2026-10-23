"""Punto de entrada: python -m loveworm"""
from loveworm.session import Session


def main() -> None:
    session = Session()
    session.login_manually()

    # cuando el login_manually devuelve, ya estás adentro
    session.like(amount=10, ratio="72.5%", sleep=5)


if __name__ == "__main__":
    main()
