"""
Общая настройка тестов research/: пути к пакету исследования и к порталу.

Тесты специально сравнивают исследовательский код с кодом портала — расхождение
между ними означает, что модель, обученная на истории, в бою получит другие числа.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESEARCH = os.path.dirname(HERE)
ROOT = os.path.dirname(RESEARCH)

for p in (RESEARCH, os.path.join(ROOT, "portal"), os.path.join(ROOT, "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)
