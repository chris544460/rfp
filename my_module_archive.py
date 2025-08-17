

"""Simple generator module for rfp_docx_apply_answers.

`gen_answer(question)` returns the question text reversed. For example:
>>> gen_answer("What's your name?")
"?eman ruoy s'tahW"
"""

def gen_answer(question: str) -> str:
    """Return the reversed question string as a toy answer generator."""
    if question is None:
        return ""
    # Reverse the entire string (maintain punctuation at end for demonstration)
    return question[::-1]