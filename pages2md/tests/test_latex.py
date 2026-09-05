from pages2md.latex import clean_latex


def test_clean_latex_removes_ocr_spacing_without_rewriting_math():
    source = (
        r"\["
        r"\mathsf {R S} _ {n, k} \left(\alpha_ {1}, \dots , \alpha_ {n}\right) = "
        r"\left\{\left(P \left(\alpha_ {1}\right), \dots , P \left(\alpha_ {n}\right)\right) "
        r"\in \mathbb {F} _ {q} ^ {n}: P \in \mathbb {F} _ {q} [ X ] \text {and} \deg P <   k "
        r"\right\}."
        r"\]"
    )

    assert clean_latex(source) == (
        r"\[\mathsf{RS}_{n, k}\left(α_1, …, α_n\right) = "
        r"\left\{\left(P\left(α_1\right), …, P\left(α_n\right)\right) "
        r"∈ 𝔽_q^n: P ∈ 𝔽_q[X] \text{and} \deg P < k\right\}.\]"
    )


def test_clean_latex_preserves_operator_and_text_spacing_and_code():
    source = r"Text `\( a_ {1} \)` and \( a_ {1} +   b_ {2}, c \)"

    assert clean_latex(source) == r"Text `\( a_ {1} \)` and \(a_1 + b_2, c\)"


def test_clean_latex_can_process_an_undelimited_formula_block():
    assert clean_latex(r"\mathbb {F} _ {q} [ X ]", assume_math=True) == r"𝔽_q[X]"


def test_clean_latex_substitutes_basic_unicode_math_symbols():
    source = r"\( \tau + \Gamma + \leq \neq \infty + \partial f \)"

    assert clean_latex(source) == r"\(τ + Γ + ≤ ≠ ∞ + ∂ f\)"
    assert clean_latex(r"\(\text {\tau} + \tau\)") == r"\(\text{\tau} + τ\)"


def test_clean_latex_substitutes_double_struck_letters():
    assert clean_latex(r"\(\mathbb {F} _ {q} \subseteq \mathbb{R}\)") == r"\(𝔽_q ⊆ ℝ\)"


def test_clean_latex_substitutes_basic_delimiter_symbols():
    source = r"\(\langle x \mid y \rangle + \left\lfloor\frac{n}{2}\right\rfloor\)"

    assert clean_latex(source) == r"\(⟨x ∣ y⟩ + \left⌊\frac{n}{2}\right⌋\)"
