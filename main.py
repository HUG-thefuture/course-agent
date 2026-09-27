#!/usr/bin/env python3
"""CourseAgent 命令行入口：python main.py <command> ..."""

import sys

from course_agent.cli import main

if __name__ == "__main__":
    sys.exit(main())
