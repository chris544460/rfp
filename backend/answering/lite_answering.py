"""Lightweight answering helper with the same signature used by legacy scripts."""
from ..ai_surface.completitions import cosine_similarity
from concurrent.futures import ThreadPoolExecutor
from ..ai_surface import Completitions
from typing import Optional
import json
import os
CURRENT_PATH = os.path.dirname(__file__)
ai_client = Completitions()

def _create_question_snippets(context_snippets: list[dict]) -> str:
    """
    Generates a formatted string containing question snippets from a list of context snippet dictionaries.
    Each selected snippet is wrapped in custom tags (<snippet_i> ... </snippet_i>), and may include:
    - A section header if the 'section' field exists and is longer than 5 characters.
    - A question line if the 'question' field exists and is longer than 5 characters.
    - The main content from the 'content' field.
    Args:
        context_snippets (list[dict]): A list of dictionaries, each representing a context snippet with possible keys:
            'selected' (bool): Whether the snippet should be included (default is True).
            'section' (str): Optional section header.
            'question' (str): Optional question text.
            'content' (str): Main content of the snippet.
    Returns:
        str: A single string containing all formatted question snippets, separated by newlines.
    """
    question_snippets: list[str] = []

    for i, snippet in enumerate(context_snippets):
        if snippet.get("selected", True):
            tmp_text = f"<snippet_{i}>\n"
            # Add section header if exists
            if len(str(snippet.get("section", ""))) > 5:
                tmp_text += f"# {snippet['section']}\n"

            # Add question if exists
            if len(str(snippet.get("question", ""))) > 5:
                tmp_text += f"**Question:** {snippet['question']}\n\n"

            # Add content (answer or snippet)
            tmp_text += snippet['content'] + '\n'

            # Close snippet tag
            tmp_text += f"</snippet_{i}>\n"
            question_snippets.append(tmp_text)

    return '\n'.join(question_snippets)

def generate_answers(questions: list[str],
                     all_context: list[list[dict]],
                     additonal_dev_instructions: Optional[str] = None,
                     no_edits: bool = False,
                     no_edit_threshold: float = 0.9
                     ) -> list[dict]:
    """
    Generates answers for a list of questions using provided context and optional instructions.
    This function first attempts to prepare direct answers for each question if `no_edits` is True and the confidence threshold is met.
    For questions without direct answers, it generates answers using a language model (LLM) with optional developer instructions.
    The final answers are merged to preserve order and preference for direct answers when available.
    Each answer is then augmented with a Grounding Confidence Index using multi-threading for efficiency.
    Args:
        questions (list[str]): List of questions to be answered.
        all_context (list[list[dict]]): List of context items for each question.
        additonal_dev_instructions (str, optional): Additional instructions for the LLM. Defaults to None.
        no_edits (bool, optional): If True, attempts direct answers without edits when confidence is high.
        no_edit_threshold (float, optional): Threshold for direct-answer confidence. Defaults to 0.9.
    Returns:
        list[dict]: List of answer dictionaries with content and metadata.
    """
    start_running_costs = ai_client.service_costs

    # Prepare direct answers if no_edits is True
    direct_answers: list[dict] = _prepare_no_edits(
        no_edits,
        questions,
        all_context,
        no_edit_threshold
    )

    # Create LLM-based answers for the remaining questions
    gpt_answers: list[dict] = _get_llm_answer(
        additonal_dev_instructions,
        questions,
        all_context,
        direct_answers
    )

    # Merge direct answers and GPT answers, preserving order
    answers: list[dict] = []
    for direct_answer in direct_answers:
        if len(direct_answer.get("content", "")) > 0:
            answers.append(direct_answer)
        else:
            answers.append(gpt_answers.pop(0))

    # Add Grounding Confidence Index to each answer
    with ThreadPoolExecutor() as executor:
        answers = list(executor.map(
            _add_confidence_to_answer,
            answers,
            all_context
        ))

    # Log costs in Azure
    print(f"Total cost of AI answering: ${ai_client.service_costs - start_running_costs:.6f}")

    return answers

def _add_confidence_to_answer(answer, context) -> dict:
    """
    Adds confidence scores to the provided answer dictionary based on context and answer properties.
    If the answer contains a 'directAnswer', returns it as-is. Otherwise calculates:
    - similarity placeholder
    - snippet density
    - LLM confidence
    - weighted GCI
    - editing score via cosine similarity of embeddings
    Updates 'scoreGCI' and 'scoreEditing' fields.
    """
    if answer.get("directAnswer", False):
        return answer

    if answer['answered']:
        similarity_scores = 1  # placeholder
        snippet_density_score = max(answer['percentUsed']) if answer['usedContext'] else 0
        llm_confidence = answer['confidenceScore']

        # Compute GCI
        used = [i for i in answer['usedContext']
                if context[i].get("@search.reranker_score") is not None]
        if used:
            gci_score = 0.5 * similarity_scores + 0.2 * snippet_density_score + 0.3 * llm_confidence
        else:
            gci_score = 0.2 * similarity_scores + 0.3 * snippet_density_score + 0.5 * llm_confidence

        answer['scoreGCI'] = gci_score

        # Compute editing score
        merged_text_used = "\n".join(context[i]['content'] for i in answer['usedContext'])
        emb_used_ans = ai_client.get_embeddings(
            [merged_text_used, answer['content']],
            model="text-embedding-3-large"
        )
        similarity = cosine_similarity(emb_used_ans[0], emb_used_ans[1])
        answer['scoreEditing'] = similarity
    else:
        answer['scoreGCI'] = None

    return answer

def _get_llm_answer(additonal_dev_instructions: Optional[str],
                    questions: list[str],
                    all_context: list[list[dict]],
                    direct_answers: list[dict]
                    ) -> list[dict]:
    """
    Generates answers via LLM for questions without direct answers.
    Loads the base prompt, injects any dev instructions, builds per-question messages,
    and calls ai_client.answers_batch with GPT-5.
    """
    with open(os.path.join(CURRENT_PATH, "prompts", "base_answer_generation.md"), 'r') as f:
        developer_message = f.read()

    if additonal_dev_instructions:
        developer_message += "\n\n# VERY IMPORTANT CONSIDERATIONS\n**When drafting the answer consider:**\n" \
                             + additonal_dev_instructions

    all_messages: list[list[dict]] = []
    for question, context, direct_answer in zip(questions, all_context, direct_answers):
        if direct_answer.get("content"):
            continue

        msgs = [
            {"promptRole": "developer", "prompt": developer_message},
            {"promptRole": "user",
             "prompt": f"The question is: {question}\n\nThe context snippets are:\n"
                       + _create_question_snippets(context)}
        ]
        all_messages.append(msgs)

    answers = ai_client.answers_batch(
        prompts=["None"] * len(all_messages),
        model="gpt-5-2025-08-07_research",
        messages=all_messages
    )

    return [json.loads(a.replace("```json", "").replace("```", "")) for a in answers]

def _prepare_no_edits(no_edits: bool,
                      questions: list[dict],
                      all_context: list[list[dict]],
                      no_edit_threshold: float
                      ) -> list[dict]:
    """
    If no_edits is True, returns direct answers from context snippets whose
    'score' ≥ no_edit_threshold, picking the single best snippet per question.
    Otherwise returns [{}] * len(questions).
    """
    direct_answers: list[dict] = []
    if no_edits:
        for snippets in all_context:
            good = sorted(
                [e for e in snippets if e['score'] >= no_edit_threshold],
                key=lambda x: x.get('updateDate', '9999'),
                reverse=True
            )
            if not good or sum(1 for s in good if s['score'] == good[0]['score']) > 1:
                direct_answers.append({})
            else:
                best = good[0]
                idx = snippets.index(best)
                direct_answers.append({
                    "content": best['content'],
                    "usedContext": [idx],
                    "scoreGCI": best['score'],
                    "percentUsed": [100],
                    "directAnswer": True,
                    "scoreEditing": 1.0
                })
    else:
        direct_answers = [{}] * len(questions)

    return direct_answers


def _prepare_no_edits(no_edits: bool, questions: list[dict], all_context: list[dict], no_edit_threshold: float) -> list[dict]:
    """
    Prepares direct answers for a set of questions based on context snippets and a similarity score threshold.
    If `no_edits` is True, the function attempts to return direct answers from context snippets whose similarity score
    meets or exceeds the specified `no_edit_threshold`. For each set of context snippets:
        - It selects snippets with a score above the threshold, sorted from most recent to oldest.
        - If there are no good snippets or multiple snippets share the highest score, an empty dictionary is returned for that question.
        - Otherwise, it returns the content of the best snippet along with metadata indicating its usage.
    If `no_edits` is False, returns a list of empty dictionaries corresponding to the number of questions.
    Args:
        no_edits (bool): Flag indicating whether to attempt returning direct answers without edits.
        questions (list[dict]): List of question dictionaries.
        all_context (list[dict]): List of lists, each containing context snippet dictionaries for a question.
        no_edit_threshold (float): Minimum similarity score required for a snippet to be considered for direct answer.
    Returns:
        list[dict]: List of answer dictionaries, each containing either the direct answer and metadata or an empty dictionary.
    """
    direct_answers: list[dict] = []
    if no_edits:
        # If the context snippets have a similarity score higher than the threshold, try return the answer as is
        for context_snippets in all_context:
            # Get good snippets and order from most recent to oldest
            good_snippets = sorted(
                [e for e in context_snippets if e['score'] >= no_edit_threshold],
                key=lambda x: x.get('updateDate','9999'),
                reverse=True
            )
            # If there are several good snippets with the same score, exit with empty
            if len(good_snippets) == 0 or len([s for s in good_snippets if s['score'] == good_snippets[0]['score']]) > 1:
                direct_answers.append({})
            else:
                direct_answers.append({
                    "content": good_snippets[0]['content'],
                    "usedContext": [context_snippets.index(good_snippets[0])],
                    "scoreGC!": good_snippets[0]['score'],
                    "percentUsed": [100],
                    "directAnswer": True,
                    "scoreEditing": 1.0
                })
    else:
        direct_answers = [{}] * len(questions)  # Empty dicts
    return direct_answers

def continue_chat(message_history: list[dict], context) -> str:
    CURRENT_PATH = os.path.dirname(__file__)

    ai_service = Completitions()

    with open(os.path.join(CURRENT_PATH, "prompts", "base_chat_answer.md"), 'r') as file:
        developer_message = file.read()

    messages = []
    # If it's the first user message, add the developer prompt
    if len([m for m in message_history if m['role'] == 'user']) == 1:
        messages.append({"promptRole": "developer", "prompt": developer_message})

    for message in message_history:
        if message['role'] == 'user':
            # Assuming the first user message is the question
            if len([m for m in messages if m['promptRole'] == 'user']) > 1:
                messages.append({"promptRole": "user", "prompt": message['content']})
            else:
                user_message = f"The question is: {message['content']}\n\nThe context snippets are:\n" + \
                    "\n".join([f"<snippet_{i} snippet_title='{e['contextTitle']}'>\n{e['content']}\n</snippet_{i}>" for i, e in enumerate(context)])
                messages.append({"promptRole": "user", "prompt": user_message})
        else:
            messages.append({"promptRole": "assistant", "prompt": message['content']})

    answer = ai_service.get_answer(
        prompt="None",  # Empty stuff
        model="gpt-5-2025-08-07_research",
        messages=messages
    )

    return answer
