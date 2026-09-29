from .webui import main

if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
