#!/bin/bash
echo "chromium: $(which chromium 2>/dev/null || echo NOT FOUND)"
echo "chromedriver: $(which chromedriver 2>/dev/null || echo NOT FOUND)"
exec python server.py