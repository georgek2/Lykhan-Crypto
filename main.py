import os
from dotenv import load_dotenv

load_dotenv()

def main():
    print("Hello from lykhan!")
    print(f"DUMMY env variable: {os.getenv('DUMMY')}")

if __name__ == "__main__":
    main()
