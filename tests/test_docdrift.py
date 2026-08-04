#!/usr/bin/env python3
"""Проверки сверки докстроки с сигнатурой. Гонять: python3 test_docdrift.py"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import docdrift


def находки(код):
    """Прогон по одному файлу без диска-дерева."""
    d = tempfile.mkdtemp()
    p = pathlib.Path(d) / "модуль.py"
    p.write_text(код, encoding="utf-8")
    try:
        return docdrift.scan_file(p, "проба")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def твёрдые(код):
    return [h for h in находки(код) if docdrift.is_hard(h)]


def мягкие(код):
    return [h for h in находки(код) if not docdrift.is_hard(h)]


# ------------------------------------------------------- класс A: переименование


class TestКлассA(unittest.TestCase):
    def test_имя_из_доки_отсутствует_в_сигнатуре(self):
        h = твёрдые('''
def f(model, n):
    """Делает дело.

    Parameters
    ----------
    MODEL : str
        Модель.
    n : int
        Сколько.
    """
''')
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["param"], "MODEL")
        self.assertEqual(h[0]["func"], "f")

    def test_совпадающие_имена_молчат(self):
        self.assertEqual(твёрдые('''
def f(model, n):
    """Делает дело.

    Parameters
    ----------
    model : str
        Модель.
    n : int
        Сколько.
    """
'''), [])

    def test_kwargs_снимает_претензии(self):
        """В функцию с **kwargs законно документируют что угодно."""
        self.assertEqual(твёрдые('''
def f(a, **kwargs):
    """Дело.

    Parameters
    ----------
    a : int
        Раз.
    whatever : str
        Два.
    """
'''), [])

    def test_self_и_cls_не_считаются_пропажей(self):
        self.assertEqual(твёрдые('''
class K:
    def m(self, a):
        """Дело.

        Parameters
        ----------
        self : K
            Он.
        a : int
            Раз.
        """
'''), [])

    def test_свойство_не_проверяется(self):
        """networkx: `G.edges` — cached_property, отдающий вызываемое
        представление. Имена nbunch и data в доке относятся к нему, а не к
        самой функции. Из 44 твёрдых находок на networkx такими были 41.
        """
        for dec in ("property", "cached_property", "functools.cached_property"):
            self.assertEqual(твёрдые(f'''
import functools
class G:
    @{dec}
    def edges(self):
        """Рёбра.

        Parameters
        ----------
        nbunch : list
            Узлы.
        data : bool
            Данные.
        """
'''), [], dec)

    def test_декоратор_совместимости_добавляет_имена(self):
        """statsmodels: `@deprecate_kwarg("random_state", "rng")`.

        Функция по-прежнему принимает старое имя, докстрока честно описывает
        оба, а сигнатура о старом не знает. Читая только сигнатуру, сканер
        объявлял живое имя несуществующим: тридцать ложных находок из
        пятидесяти шести. Случай принесён потоком 04.08.2026.
        """
        self.assertEqual(твёрдые('''
@deprecate_kwarg("random_state", "rng")
def rvs(self, size, rng=None):
    """Дело.

    Parameters
    ----------
    random_state : int
        Старое имя.
    rng : int
        Новое имя.
    """
'''), [])

    def test_декоратор_совместимости_словарём(self):
        self.assertEqual(твёрдые('''
@renamed_kwargs({"old_name": "new_name"})
def f(new_name=None):
    """Дело.

    Parameters
    ----------
    old_name : int
        Раз.
    """
'''), [])

    def test_декоратор_совместимости_ключевым_словом(self):
        self.assertEqual(твёрдые('''
@deprecated_alias(old_name="new_name")
def f(new_name=None):
    """Дело.

    Parameters
    ----------
    old_name : int
        Раз.
    """
'''), [])

    def test_непрозрачный_декоратор_снимает_весь_класс_A(self):
        """Декоратор совместимости есть, а какие имена берёт — не видно.

        Утверждать «такого имени нет», зная источник неполно, нельзя. Это
        та же порода Г, что матрица CI за `${{ env.MIN_PYTHON }}`.
        """
        self.assertEqual(твёрдые('''
@deprecated
def f(new_name=None):
    """Дело.

    Parameters
    ----------
    old_name : int
        Раз.
    """
'''), [])

    def test_декоратор_совместимости_не_молчит_про_чужое_имя(self):
        """Снимаются только имена, которые декоратор действительно называет."""
        h = твёрдые('''
@deprecate_kwarg("random_state", "rng")
def rvs(self, size, rng=None):
    """Дело.

    Parameters
    ----------
    random_state : int
        Старое имя.
    небывалое : int
        Просто выдумка.
    """
''')
        self.assertEqual([x["param"] for x in h], ["небывалое"])

    def test_пометка_TODO_не_аргумент(self):
        """statsmodels `gradient_momcond`: строка `TODO: looks like not used
        yet` внутри раздела Parameters по форме неотличима от `имя : тип`."""
        self.assertEqual(твёрдые('''
def f(params):
    """Дело.

    Parameters
    ----------
    params : ndarray
        Раз.

    TODO: looks like not used yet
    FIXME: и это тоже
    """
'''), [])

    def test_обычный_декоратор_не_снимает_проверку(self):
        """Пропускать всё декорированное было бы слишком широко."""
        h = твёрдые('''
def deco(f):
    return f

@deco
def f(model):
    """Дело.

    Parameters
    ----------
    MODEL : str
        Модель.
    """
''')
        self.assertEqual(len(h), 1)

    def test_раздел_из_примера_не_считается(self):
        """mne: в doctest печатают чужую докстроку целиком, и её раздел
        Parameters выходит без отступа — неотличим от настоящего.
        Так `copy_function_doc_to_method_doc` получил аргументы `a` и `b`.
        """
        self.assertEqual(твёрдые('''
def f(source):
    """Дело.

    Parameters
    ----------
    source : function
        Откуда.

    Examples
    --------
    >>> print(other.__doc__)
    Docstring.
    <BLANKLINE>
    Parameters
    ----------
    a : int
        Раз.
    b : int
        Два.
    """
'''), [])

    def test_другие_разделы_до_примеров_читаются(self):
        """`Other Parameters` — законный раздел и стоит до Examples."""
        h = твёрдые('''
def f(a):
    """Дело.

    Parameters
    ----------
    a : int
        Раз.

    Other Parameters
    ----------------
    небывалый : int
        Два.
    """
''')
        self.assertEqual([x["param"] for x in h], ["небывалый"])

    def test_google_style_не_разбирается(self):
        """Заявлено в шапке: разбираем только numpydoc."""
        self.assertEqual(находки('''
def f(model):
    """Дело.

    Args:
        MODEL: модель
    """
'''), [])

    def test_докстроки_нет(self):
        self.assertEqual(находки("def f(a):\n    return a\n"), [])

    def test_битый_файл_не_роняет(self):
        self.assertEqual(находки("def f(:\n"), [])


# ------------------------------------------- класс B: значение по умолчанию


class TestКлассB(unittest.TestCase):
    def test_расхождение_умолчания(self):
        m = мягкие('''
def f(alpha=0.01):
    """Дело.

    Parameters
    ----------
    alpha : float, default 0
        Шаг.
    """
''')
        self.assertEqual(len(m), 1)
        self.assertEqual(m[0]["real_default"], "0.01")

    def test_совпадающее_умолчание_молчит(self):
        self.assertEqual(мягкие('''
def f(alpha=0.01):
    """Дело.

    Parameters
    ----------
    alpha : float, default 0.01
        Шаг.
    """
'''), [])

    def test_разное_написание_одного_значения(self):
        """`True` и `true`, кавычки вокруг строки — это одно и то же."""
        self.assertEqual(мягкие('''
def f(flag=True, name="x"):
    """Дело.

    Parameters
    ----------
    flag : bool, default true
        Раз.
    name : str, default 'x'
        Два.
    """
'''), [])

    def test_часовой_None_молчит(self):
        """networkx: в коде None, в доке — во что он превращается.

        «create_using : graph type, default nx.Graph» при `create_using=None`
        это идиома всего языка, а не расхождение. На networkx таких находок
        было 80 из 168.
        """
        self.assertEqual(мягкие('''
def f(create_using=None):
    """Дело.

    Parameters
    ----------
    create_using : graph type, default nx.Graph
        Тип.
    """
'''), [])

    def test_проза_вместо_значения_молчит(self):
        for текст in ("all nodes in G", "len(G", "first node in list(G"):
            self.assertEqual(мягкие(f'''
def f(n=1):
    """Дело.

    Parameters
    ----------
    n : int, default {текст}
        Раз.
    """
'''), [], текст)

    def test_одно_число_в_разной_записи(self):
        """`1e-8` против `1e-08` и восьмеричное `0o775` против `509`."""
        for док, код in (("1e-8", "1e-08"), ("1.0e-6", "1e-06"), ("0o775", "0o775")):
            self.assertEqual(мягкие(f'''
def f(x={код}):
    """Дело.

    Parameters
    ----------
    x : число, default {док}
        Раз.
    """
'''), [], док)

    def test_логическое_не_равно_единице(self):
        """True и 1 — разные умолчания, числовое сравнение их путать не должно."""
        self.assertEqual(len(мягкие('''
def f(flag=1):
    """Дело.

    Parameters
    ----------
    flag : bool, default True
        Раз.
    """
''')), 1)

    def test_дробное_умолчание_не_обрезается(self):
        """`default 0.01` читалось как `0`: точка стояла в списке запретов,
        и каждое дробное умолчание становилось расхождением."""
        self.assertEqual(мягкие('''
def f(alpha=0.01):
    """Дело.

    Parameters
    ----------
    alpha : float, default 0.01
        Шаг.
    """
'''), [])

    def test_вычисляемое_умолчание_пропускается(self):
        """`ast.literal_eval` на выражении молчит, и это правильно."""
        self.assertEqual(мягкие('''
СТО = 100

def f(n=СТО):
    """Дело.

    Parameters
    ----------
    n : int, default 1
        Раз.
    """
'''), [])

    def test_умолчание_мягкое_а_не_твёрдое(self):
        """Сравнивается вольный текст с литералом — решает человек."""
        h = находки('''
def f(alpha=0.01):
    """Дело.

    Parameters
    ----------
    alpha : float, default 0
        Шаг.
    """
''')
        self.assertTrue(h)
        self.assertFalse(any(docdrift.is_hard(x) for x in h))


# ------------------------------------------------------------ договор набора


class TestДоговор(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def дерево(self, rel, код):
        p = pathlib.Path(self.dir) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(код, encoding="utf-8")

    ДРЕЙФ = '''
def f(model):
    """Дело.

    Parameters
    ----------
    MODEL : str
        Модель.
    """
'''

    def test_json_несёт_hard_и_код_возврата(self):
        self.дерево("пакет/модуль.py", self.ДРЕЙФ)
        out = os.path.join(self.dir, "out.json")
        code = docdrift.main([self.dir, "--json", out])
        self.assertEqual(code, 1)
        data = json.load(open(out, encoding="utf-8"))
        self.assertTrue(any(x["hard"] for x in data))

    def test_чисто_значит_ноль(self):
        self.дерево("пакет/модуль.py", "def f(a):\n    return a\n")
        self.assertEqual(docdrift.main([self.dir]), 0)

    def test_каталоги_тестов_и_примеров_пропускаются(self):
        for rel in ("tests/модуль.py", "docs/модуль.py", "examples/модуль.py",
                    "пакет/test_модуль.py", "пакет/модуль_test.py", "пакет/conftest.py"):
            self.дерево(rel, self.ДРЕЙФ)
        self.assertEqual(docdrift.scan(self.dir), [])

    def test_общий_список_каталогов_наследуется(self):
        """Свой список каталогов однажды разошёлся у трёх инструментов."""
        import common
        self.assertTrue(common.SKIP_DIRS <= docdrift.SKIP_DIRS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
