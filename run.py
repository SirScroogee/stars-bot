#!/usr/bin/env python
"""Entry point for running the bot."""
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

if __name__ == "__main__":
    from src.bot.main import main
    import asyncio
    asyncio.run(main())
