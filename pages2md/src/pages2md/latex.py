from __future__ import annotations

import re


_CODE_SPAN = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.DOTALL)
_MATH_SPAN = re.compile(
    r"\\\[.*?\\\]|\\\(.*?\\\)|\$\$.*?\$\$|"
    r"\\begin\{(?P<environment>[A-Za-z*]+)\}.*?\\end\{(?P=environment)\}|"
    r"(?<!\\)\$(?!\$)(?:[^$\\\n]|\\.)*?(?<!\\)\$(?!\$)",
    re.DOTALL,
)
_TEXT_GROUP = re.compile(
    r"\\(?:text|textbf|textit|textrm|textsf|texttt|operatorname)\{[^{}]*\}"
)
_MATH_FONT_GROUP = re.compile(
    r"(\\(?:mathbb|mathbf|mathsf|mathrm|mathcal|mathit|mathtt)\{)([A-Za-z0-9 \t\n]+)(\})"
)
_UNICODE_COMMANDS = {
    # Greek letters, including the distinct LaTeX variant forms.
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ϵ", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "varkappa": "ϰ", "lambda": "λ", "mu": "μ",
    "nu": "ν", "xi": "ξ", "omicron": "ο", "pi": "π", "varpi": "ϖ",
    "rho": "ρ", "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ",
    "upsilon": "υ", "phi": "φ", "varphi": "ϕ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
    # Unambiguous relations, operators, and common symbols.
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "equiv": "≡", "approx": "≈", "sim": "∼", "pm": "±", "times": "×", "cdot": "⋅",
    "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆", "supset": "⊃", "supseteq": "⊇",
    "cup": "∪", "cap": "∩", "setminus": "∖", "to": "→", "rightarrow": "→", "leftarrow": "←",
    "mapsto": "↦", "Rightarrow": "⇒", "Leftarrow": "⇐", "Leftrightarrow": "⇔",
    "forall": "∀", "exists": "∃", "infty": "∞", "partial": "∂", "nabla": "∇",
    "dots": "…", "ldots": "…", "cdots": "⋯", "vdots": "⋮", "ddots": "⋱",
    "langle": "⟨", "rangle": "⟩", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉",
    "mid": "∣", "vert": "∣", "Vert": "‖", "parallel": "∥",
}
_UNICODE_COMMAND = re.compile(
    r"\\(?:" + "|".join(sorted(map(re.escape, _UNICODE_COMMANDS), key=len, reverse=True)) + r")(?![A-Za-z])"
)
_MATHBB_GROUP = re.compile(r"\\mathbb\{([A-Za-z])\}")
_SINGLE_SCRIPT = re.compile(r"([_^])\{([A-Za-z0-9α-ωΑ-Ω])\}")
_DOUBLE_STRUCK = {
    **dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "𝔸𝔹ℂ𝔻𝔼𝔽𝔾ℍ𝕀𝕁𝕂𝕃𝕄ℕ𝕆ℙℚℝ𝕊𝕋𝕌𝕍𝕎𝕏𝕐ℤ")),
    **dict(zip("abcdefghijklmnopqrstuvwxyz", "𝕒𝕓𝕔𝕕𝕖𝕗𝕘𝕙𝕚𝕛𝕜𝕝𝕞𝕟𝕠𝕡𝕢𝕣𝕤𝕥𝕦𝕧𝕨𝕩𝕪𝕫")),
}


def clean_latex(markdown: str, *, assume_math: bool = False) -> str:
    """Clean harmless OCR whitespace inside Markdown's LaTeX math spans.

    This deliberately does not remove grouping braces or rewrite structural
    commands. It normalizes harmless whitespace and selected Unicode-equivalent
    commands, while leaving text-command contents and Markdown code intact.
    """
    if assume_math and not _MATH_SPAN.search(markdown):
        return _clean_math(markdown)

    result: list[str] = []
    cursor = 0
    for code in _CODE_SPAN.finditer(markdown):
        result.append(_clean_math_spans(markdown[cursor : code.start()]))
        result.append(code.group(0))
        cursor = code.end()
    result.append(_clean_math_spans(markdown[cursor:]))
    return "".join(result)


def _clean_math_spans(value: str) -> str:
    result: list[str] = []
    cursor = 0
    for match in _MATH_SPAN.finditer(value):
        result.append(value[cursor : match.start()])
        result.append(_clean_math(match.group(0)))
        cursor = match.end()
    result.append(value[cursor:])
    return "".join(result)


def _clean_math(value: str) -> str:
    value = re.sub(r"\\([A-Za-z]+)[ \t]+(?=\{)", r"\\\1", value)
    value = re.sub(r"(?<=[_^])[ \t]+(?=\{)", "", value)
    value = re.sub(r"(\\(?:left|right))[ \t]+", r"\1", value)
    value = re.sub(r"[ \t]+(?=[_^])", "", value)
    value = re.sub(r"([A-Za-z0-9}\)])[ \t]+(?=\\(?:left|right)\b)", r"\1", value)
    value = re.sub(r"([A-Za-z0-9}])[ \t]+(?=\[)", r"\1", value)

    protected: list[str] = []

    def protect_text(match: re.Match[str]) -> str:
        protected.append(match.group(0))
        return f"\x00pages2md-text-{len(protected) - 1}\x00"

    value = _TEXT_GROUP.sub(protect_text, value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"[ \t]+([,.;!?])", r"\1", value)
    value = re.sub(r"([\(\[])[ \t]+", r"\1", value)
    value = re.sub(r"[ \t]+([\)\]])", r"\1", value)
    value = re.sub(r"(?<!\\)\{[ \t]+", "{", value)
    value = re.sub(r"[ \t]+\}(?!\})", "}", value)
    value = _MATH_FONT_GROUP.sub(_clean_math_font_group, value)
    value = _MATHBB_GROUP.sub(lambda match: _DOUBLE_STRUCK[match.group(1)], value)
    value = _UNICODE_COMMAND.sub(lambda match: _UNICODE_COMMANDS[match.group(0)[1:]], value)
    value = re.sub(r"([⟨⌊⌈])[ \t]+", r"\1", value)
    value = re.sub(r"[ \t]+([⟩⌋⌉])", r"\1", value)
    value = _SINGLE_SCRIPT.sub(r"\1\2", value)
    value = re.sub(r"^(\\\[|\\\(|\$\$|\$)[ \t\n]+", r"\1", value)
    value = re.sub(r"[ \t\n]+(\\\]|\\\)|\$\$|\$)$", r"\1", value)
    for index, text in enumerate(protected):
        value = value.replace(f"\x00pages2md-text-{index}\x00", text)
    return value


def _clean_math_font_group(match: re.Match[str]) -> str:
    content = re.sub(r"[ \t\n]+", "", match.group(2))
    return f"{match.group(1)}{content}{match.group(3)}"
