"""Small read-only S-expression reader retaining top-level source spans."""

import re


class Node(list):
    def __init__(self, start):
        super().__init__()
        self.start = start
        self.end = None


def parse(text):
    stack = []
    root = None
    for match in re.finditer(r'"(?:\\.|[^"\\])*"|[()]|[^\s()]+', text):
        token = match.group()
        if token == "(":
            node = Node(match.start())
            if stack:
                stack[-1].append(node)
            elif root is not None:
                raise ValueError("Multiple S-expression roots")
            else:
                root = node
            stack.append(node)
        elif token == ")":
            if not stack:
                raise ValueError("Unmatched closing parenthesis")
            stack.pop().end = match.end()
        else:
            if not stack:
                raise ValueError("Atom outside S-expression")
            if token.startswith('"'):
                token = re.sub(r'\\([\\"])', r'\1', token[1:-1])
            stack[-1].append(token)
    if stack or root is None:
        raise ValueError("Incomplete S-expression")
    return root


def children(node, key):
    return [n for n in node if isinstance(n, Node) and n and n[0] == key]


def child(node, key, default=None):
    return next(iter(children(node, key)), default)


def xy(node, key="at"):
    value = child(node, key)
    if value is None or len(value) < 3:
        raise ValueError(f"Missing coordinate {key}")
    return float(value[1]), float(value[2])
