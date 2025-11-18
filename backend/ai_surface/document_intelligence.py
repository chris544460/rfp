from langchain.text_splitter import MarkdownHeaderTextSplitter
from concurrent.futures import ThreadPoolExecutor
from ..ai_surface import Completitions
from requests.auth import HTTPBasicAuth
from io import BytesIO
from time import sleep
import datetime as dt
import numpy as np
import requests
import base64
import uuid
import json
import os
import io
import re
import os

# Load config file
with open(os.path.join(os.path.dirname(__file__), 'config.json'), 'r') as file:
    config = json.load(file)

def fix_footers(content: str, paragraphs: list[dict]) -> tuple[str, list[dict]]:
    # Get the identified footers
    footers = [p for p in paragraphs if p.get('role') == 'pageFooter']

    # Are there any missing footers? – They are single lines with any of the already identified footers
    footers_to_fix = []
    for original_footer in footers:
        if re.search(r'".*"', original_footer['content']) is None:
            continue

        # Get the text without the quotes
        footer_text = re.search(r'".*"', original_footer['content']).group(0).replace('"','')

        # Where are the typical footers?
        typical_polygon = [e['boundingRegions'][0]['polygon'] for e in footers if e['content'] == original_footer['content']][0]

        # Look for possible fixes
        possible_footers = [p for p in paragraphs if p.get('role') is None and p['content'] == footer_text]
        for possible_footer in possible_footers:
            possible_polygon = possible_footer['boundingRegions'][0]['polygon']
            similarity = cosine_similarity([possible_polygon], typical_polygon).mean()

            # If the possible footer is close to the typical position, reformat it's text
            if similarity > 0.8:
                footers_to_fix.append(possible_footer)
                possible_footer['role'] = 'pageFooter'
                possible_footer['needsFixing'] = True

    # In reverse order, fix the footers in the content (in reverse to be able to trace the edited content)
    footers_to_fix = sorted(footers_to_fix, key = lambda x: x['spans'][0]['offset'], reverse = True)
    for footer in footers_to_fix:
        start = footer['spans'][0]['offset']
        end = footer['spans'][0]['offset'] + footer['spans'][0]['length']

        footer['content'] = f'<!-- PageFooter="{footer["content"]}" -->'
        footer['spans'][0]['length'] = len(footer['content'])

        additional_length = footer['spans'][0]['length'] - (end - start)

        content = content[:start] + footer['content'] + content[end:]

        # Need to update all the subsequent paragraphs
        current_paragraph = paragraphs.index(footer)
        for i in range(current_paragraph+1, len(paragraphs)):
            if 'spans' in paragraphs[i]:
                paragraphs[i]['spans'][0]['offset'] += additional_length

    # Return the modified variables
    return content, paragraphs

def fix_broken_paragraphs(content: str, paragraphs: list[dict]) -> tuple[str, list[dict]]:
    content_paragraphs = [p for p in paragraphs if p.get('role') is None and p['content'].strip() != '']

    fixed_paragraph = False
    for p_num, paragraph in enumerate(content_paragraphs):
        if fixed_paragraph == False:
            last_paragraph = content_paragraphs[p_num-1]

        if p_num == 0:
            continue

        current_page = paragraph['boundingRegions'][0]['pageNumber']
        last_page = last_paragraph['boundingRegions'][0]['pageNumber']

        current_content = paragraph['content']
        last_content = last_paragraph['content']

        # Does the paragraph look like a continuation of the last page's one?
        if not(current_page >= last_page and current_content[0].islower() and last_content[-1] not in ['.',',','?','!']):
            fixed_paragraph = False
            continue

        # If the paragraph is a continuation of the last one, append it to the last one
        fixed_paragraph = True

        # Check if the last paragraph needs a space
        if last_content.endswith(' '):
            last_paragraph['content'] += current_content
            additional_length = 0
        else:
            last_paragraph['content'] += ' ' + current_content
            additional_length = 1

        # Delete text from content of current paragraph
        start = paragraph['spans'][0]['offset']
        end = paragraph['spans'][0]['offset'] + paragraph['spans'][0]['length']
        content = content[:start] + content[end:]
        paragraph['content'] = ''
        paragraph['spans'][0]['length'] = 0

        # Update attributes of past paragraph
        start = last_paragraph['spans'][0]['offset']
        end = last_paragraph['spans'][0]['offset'] + last_paragraph['spans'][0]['length']
        content = content[:start] + last_paragraph['content'] + content[end:]
        last_paragraph['spans'][0]['length'] = len(last_paragraph['content'])

        # Add bounding region
        last_paragraph['boundingRegions'].append(paragraph['boundingRegions'][0])

        # Update all subsequent paragraphs
        current_paragraph = paragraphs.index(last_paragraph)
        deleted_paragraph = paragraphs.index(paragraph)

        for i in range(current_paragraph+1, len(paragraphs)):
            if 'spans' in paragraphs[i]:
                paragraphs[i]['spans'][0]['offset'] += additional_length + (len(current_content) if i < deleted_paragraph else 0)

    return content, paragraphs

def tag_paragraphs_in_table(adi_result: dict):
    if 'tables' not in adi_result:
        return None

    paragraphs = adi_result['paragraphs']
    tables = adi_result['tables']

    for table_num, table in enumerate(tables):
        for n_cells, cell in enumerate(table['cells']):
            if 'elements' in cell and cell['elements']:
                # Extract paragraph number once
                paragraph_number = int(cell['elements'][0].split('/')[-1])
                cell_bounding_region = cell['boundingRegions'][0]['polygon']

                # Update paragraph properties in a single step
                paragraphs[paragraph_number].update({
                    'inTable': True,
                    'cellRow': cell['rowIndex'],
                    'cellColumn': cell['columnIndex'],
                    'cellWidth': cell_bounding_region[1] - cell_bounding_region[0],
                    'tableNumber': table_num,
                    'cellNumber': n_cells
                })

def fix_adi_response(adi_results):
    content = adi_results['analyzeResult']['content']
    paragraphs = adi_results['analyzeResult']['paragraphs']

    # Merge tables that are across pages
    merge_cross_page_tables(adi_results['analyzeResult'])

    # Tag paragraphs that are cells in tables
    tag_paragraphs_in_table(adi_results['analyzeResult'])

    # Fix possible scan issues in the boundaries of the page
    # cut_x = np.percentile([p['boundingRegions'][0]['polygon'][0] for i, p in enumerate(paragraphs)],0.2)
    # paragraphs = [p for p in paragraphs if not(p['boundingRegions'][0]['polygon'][0] <= cut_x and p['content'].isdigit())]

    # Fix missing footers based on position and content
    content, paragraphs = fix_footers(content, paragraphs)

    # Fix broken paragraphs
    content, paragraphs = fix_broken_paragraphs(content, paragraphs)

    return paragraphs, content

class ADI:
    def __init__(self):
        if 'aladdin_user' not in os.environ:
            from blkcore.user import get_user
            os.environ['aladdin_user'] = get_user()
        if 'aladdin_passwd' not in os.environ:
            from blkcore.sso import get_auth_passwd
            os.environ['aladdin_passwd'] = get_auth_passwd()

        # Save the basic authentication
        self.hard_code_auth = HTTPBasicAuth(os.environ['aladdin_user'], os.environ['aladdin_passwd'])

        # Environment
        self.default_web_server = os.environ.get('defaultWebServer', 'https://webster.bfm.com')

        # Save the API Key
        self.api_key = os.environ['aladdin_studio_api_key']

        # Cost per 1,000 pages
        self.models = {
            'prebuilt-read': 1.5,
            'prebuilt-layout': 10,
            'prebuilt-layout-2024-11-30': 10,
            'prebuilt-read-2024-11-30': 1.5
        }

        self.cache_container = 'adi-responses'

    def analyze_document(self, document: io.BufferedReader, model_id: str = 'prebuilt-layout-2024-11-30',
                         output_format: str = 'text', query_fields: list[str] = None, need_high_resolution: bool = False) -> dict:
        """
        Extracts information from a document using the Azure Document Intelligence API.
        Args:
            document (bytes): The document to be processed, encoded as bytes.
            model_id (str, optional): The ID of the model to be used for extraction. Defaults to 'prebuilt-layout-2024-11-30'. Valid options are:
                - 'prebuilt-read': Extracts text from the document.
                - 'prebuilt-layout': Extracts text and layout information from the document.
            output_format (str, optional): The desired output format. Defaults to 'text'. Valid options are:
                - 'text': Extracts the text from the document.
                - 'markdown': Extracts the text from the document in markdown format.
        Raises:
            ValueError: If the document is None.
        Returns:
            dict: The extracted information from the document.
        """
        if document is None:
            raise ValueError("Document is required")
        else:
            encoded_document = base64.b64encode(document.read())

        # Check if file size is more than 20mb and if so, reduce it
        encoded_size = len(encoded_document) / (1024 * 1024)
        if len(encoded_document) / (1024 * 1024) > 10:
            n_parts = int(encoded_size // 8) + 1
            new_pdf_parts = split_pdf(document, parts=n_parts)
            for part in new_pdf_parts:
                part.seek(0)
            if max([len(base64.b64encode(part.read())) for part in new_pdf_parts]) > 9 * 1024 * 1024:
                n_parts = int(encoded_size // 3) + 1
                new_pdf_parts = split_pdf(document, parts=n_parts)
        else:
            new_pdf_parts = [document]

        parts_results = []
        for part in new_pdf_parts:
            part.seek(0)
            encoded_part = base64.b64encode(part.read())
            part_result = self.make_adi_request(encoded_document=encoded_part, model_id=model_id,
                                                output_format=output_format, query_fields=query_fields,
                                                need_high_resolution=need_high_resolution)
            parts_results.append(part_result)

        results = merge_adi_results(parts_results)
        return results

    def make_adi_request(self, encoded_document, model_id, output_format, query_fields, need_high_resolution):
        # Prepare parameters to send the request
        url = f"{self.default_web_server}/api/ai-platform/nlp/document-extraction/v1/documentExtraction:generate"
        headers = {
            "Content-Type": "application/json",
            "VND.com.blackrock.Request-ID": str(uuid.uuid1()),
            "VND.com.blackrock.Origin-Timestamp": str(dt.datetime.utcnow().replace(microsecond=0).astimezone().isoformat()),
            "VND.com.blackrock.API-Key": self.api_key
        }





    def make_adi_request(self, encoded_document, model_id, output_format, query_fields, need_high_resolution):
        # Prepare parameters to send the request
        url = f"{self.default_web_server}/api/ai-platform/nlp/document-extraction/v1/documentExtraction:generate"

        headers = {
            "Content-Type": "application/json",
            "VND.com.blackrock.Request-ID": str(uuid.uuid1()),
            "VND.com.blackrock.Origin-Timestamp": str(dt.datetime.utcnow().replace(microsecond=0).astimezone().isoformat()),
            "VND.com.blackrock.API-Key": self.api_key
        }

        payload = {
            "base64source": encoded_document.decode(),
            "modelId": model_id,
            "modelParam": {
                "azuredoc": {
                    "format": output_format,
                    "features": []
                }
            }
        }

        if need_high_resolution:
            payload['modelParam']['azuredoc']['features'].append("ocrHighResolution")

        if query_fields is not None:
            payload['modelParam']['azuredoc']['queryFields'] = query_fields
            payload['modelParam']['azuredoc']['features'].append('queryFields')

        # Send the request
        response = requests.post(url, json=payload, headers=headers, auth=self.hard_code_auth)

        data = response.json()

        finished = data['done']
        lro_id = data['id']

        if not finished:
            lro_url = f"{self.default_web_server}/api/ai-platform/nlp/document-extraction/v1/longRunningOperations/{lro_id}"
            data = self.get_lro_response(lro_url, headers)

        results = json.loads(data['response']['documentExtraction'])

        return results

    def encode_document(
        self,
        document: io.BufferedReader,
        owner_id: str,
        model_id: str = 'prebuilt-layout-2024-11-30',
        need_high_resolution: bool = False,
    ) -> dict:

        ini_time = dt.datetime.now()

        # First send request to Azure and get the main content
        document.seek(0)
        adi_results = self.analyze_document(
            document=document,
            model_id=model_id,
            output_format='markdown',
            need_high_resolution=need_high_resolution
        )

        if adi_results is None:
            return {}

        # Clean and fix ADI results
        adi_results['analyzeResult']['paragraphs'], adi_results['analyzeResult']['content'] = fix_adi_response(adi_results)

        # Add file name and owner id
        adi_results['fileName'] = os.path.basename(document.name) if hasattr(document, 'name') else 'unknown_document.pdf'
        adi_results['ownerId'] = owner_id

        # Create chunks
        adi_results['analyzeResult']['chunks'] = create_chunks(adi_results)

        print(f"ADI took {dt.datetime.now() - ini_time} seconds to encode.")
        # @TODO: Save in Azure container

        return adi_results['analyzeResult']

    def get_lro_response(self, request_url: str, headers: dict, seconds_sleep: int = 5):
        refresh = 1
        while refresh < 50:
            try:
                r = requests.get(request_url, headers=headers, auth=self.hard_code_auth)
                updated_data = r.json()
                if updated_data['done']:
                    return updated_data
            except:
                print('Error in request... retrying in 15 seconds.')
                sleep(15)
            else:
                sleep(seconds_sleep)
                refresh += 1

            if refresh == 20:
                seconds_sleep = 10

    def create_chunks(adi_results):
        """
        Splits the content of a PDF or Word document into structured chunks based on section headers,
        preparing them for use in an AI Search Index.
        The function processes the document content, identifies section headers (Header 1 to Header 4),
        and splits the text accordingly. For each chunk, it generates a markdown-formatted header,
        computes embeddings, and extracts relevant keywords in parallel. The resulting chunks are
        structured as dictionaries containing metadata, content, keywords, embeddings, and other
        relevant fields for indexing.
        Args:
            adi_results (dict): A dictionary containing the analyzed results of the document,
                                including the full text content, file name, and owner ID.
        Returns:
            list[dict]: A list of dictionaries, each representing a chunk of the document with
                        associated metadata, content, keywords, and embeddings, suitable for ingestion
                        into an AI Search Index.
        """

        def create_markdown_header(document):
            "Create a markdown header from the document metadata"
            output = ''

            for level, header in document.metadata.items():
                header_level = int(level.replace('Header ', ''))
                output += f"{'#' * header_level} {header}\n"

            return output

        def create_chunk_id(file_name: str, chunk_number: int) -> str:
            "Create a safe id for Azure AI Search"
            safe_file_name = re.sub(r'[^a-zA-Z0-9]', '_', file_name)
            return f"{adi_results['ownerId']}_{safe_file_name}_chunk_{chunk_number}"

        # Initialize client to get embeddings and key words
        ai_client = Completions()

        # Initialize variable to save chunks
        chunks: list[dict] = []

        # Define how to split the document
        headers_to_split_on = [
            ("#", "Header 1"),
            ("##", "Header 2"),
            ("###", "Header 3"),
            ("####", "Header 4"),
        ]

        text_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=headers_to_split_on)
        splits = text_splitter.split_text(adi_results['analyzeResult']['content'])

        # Get embeddings and key words in parallel
        with ThreadPoolExecutor() as executor:
            embedding_futures = executor.submit(
                ai_client.get_embeddings,
                [f"{create_markdown_header(split)}\n\n{split.page_content}" for split in splits],
                config['embeddingModel']
            )

            keyword_futures = executor.submit(
                ai_client.answers_batch,
                [f"From the following text, give me the 5 to 20 most relevant keywords, separated by commas and nothing else:\n\n{document.page_content}" for document in splits],
                model='gpt-5-nano-2025-08-07_research'
            )

        # Collect results
        embeddings = embedding_futures.result()
        all_keywords = keyword_futures.result()

        # Create chunks
        for i, document in enumerate(splits):
            chunks.append({
                "id": create_chunk_id(adi_results['fileName'], i),
                "content": f"{create_markdown_header(document)}\n\n{document.page_content}",
                "section": ' '.join(document.metadata.values()),
                "tags": [],
                "source": adi_results['fileName'],
                "approvedSource": False,
                "keywords": all_keywords[i].split(','),
                "embedding": embeddings[i],
                "ownerId": adi_results['ownerId']
            })

        return chunks

    def find_merge_table_candidates(tables, paragraphs) -> dict:

        merge_tables_candidates = []
        pre_table_idx = -1
        pre_table_page = -1

        for table_idx, table in enumerate(tables):
            table_page = min([region['pageNumber'] for region in table['boundingRegions']])

            # If there is a table on the next page, it is a candidate for merging with the previous table.
            if table_page == pre_table_page + 1:
                pre_table = {"pre_table_idx": pre_table_idx}
                merge_tables_candidates.append(pre_table)

            pre_table_idx = table_idx
            pre_table_page = table_page

        table_merges = {}
        for candidate in merge_tables_candidates:
            table_idx = candidate["pre_table_idx"]

            # If there is no paragraph within the range and the columns of the tables match, merge the tables.
            to_n_columns = tables[table_idx]['columnCount']
            to_headers = [t['content'] for t in tables[table_idx]['cells'] if t.get('kind') == 'columnHeader']
            to_page_number = tables[table_idx]['boundingRegions'][0]['pageNumber']
            to_offset = tables[table_idx]['spans'][0]['offset'] + tables[table_idx]['spans'][0]['length']
            to_last_paragraph_number = [c for c in tables[table_idx]['cells'] if 'elements' in c][-1]['elements'][0].split('/')[-1]

            from_n_columns = tables[table_idx + 1]['columnCount']
            from_headers = [t['content'] for t in tables[table_idx + 1]['cells'] if t.get('kind') == 'columnHeader']
            from_page_number = tables[table_idx + 1]['boundingRegions'][0]['pageNumber']
            try:
                from_offset = tables[table_idx + 1]['spans'][0]['offset']
            except:
                from_offset = 0

            try:
                from_last_paragraph_number = [c for c in tables[table_idx + 1]['cells'] if 'elements' in c][-1]['elements'][0].split('/')[-1]
            except:
                from_last_paragraph_number = 0

            n_headers_between = len([p for p in paragraphs[int(to_last_paragraph_number):int(from_last_paragraph_number)] if \
                (p.get('role') == 'sectionHeading' or p.get('role') == 'subSectionHeading' or p.get('role') == 'title') and \
                p['boundingRegions'][0]['pageNumber'] == to_page_number])

            if from_n_columns == to_n_columns and from_page_number == to_page_number + 1 and abs(from_offset - to_offset) < 200 and (from_headers == to_headers or len(from_headers) == 0) and n_headers_between == 0:
                table_merges[table_idx + 1] = table_idx

        return table_merges

    def merge_cross_page_tables(adi_result):
        tables = adi_result.get('tables', [])
        paragraphs = adi_result['paragraphs']
        table_merges = find_merge_table_candidates(tables, paragraphs)

        for table_idx, merge_idx in table_merges.items():
            # Skip if it's the same table
            if table_idx == merge_idx:
                continue

            # Get source and target headers to assess if it's table continuation
            to_headers = [cell['content'] for cell in tables[merge_idx]['cells'] if cell.get('kind') == 'columnHeader']
            from_headers = [cell['content'] for cell in tables[table_idx]['cells'] if cell.get('kind') == 'columnHeader']

            # If it's a collateral/credit Schedule table then skip
            if len(list(set(to_headers) & set(['Party A', 'Party B', 'Valuation Percentage', 'Valuation', 'Percentage']))) >= 3:
                continue

            # If table headers do not match or the source headers are not empty, skip
            if not(to_headers == from_headers or len(from_headers) == 0):
                continue

            print(f"Table {table_idx} will be merged with table {merge_idx}")
            cells_to_move = [c for c in tables[table_idx]['cells']]
            # Update the row index and table number for the cells to move, also update in the paragraph object
            for cell in cells_to_move:
                cell['rowIndex'] += tables[merge_idx]['rowCount']
                if cell.get('kind') == 'columnHeader':
                    cell['kind'] = 'columnHeaderFromMerged'

            tables[merge_idx]['cells'].extend(cells_to_move)
            tables[merge_idx]['rowCount'] += tables[table_idx]['rowCount']
            tables[merge_idx]['spans'].extend(tables[table_idx]['spans'])
            tables[merge_idx]['boundingRegions'].extend(tables[table_idx]['boundingRegions'])

        # Now remove the merged tables in reverse order
        for table_idx in sorted(table_merges.keys(), reverse=True):
            tables.pop(table_idx)


def split_pdf_by_size(pdf_file: io.BufferedReader) -> io.BytesIO:
    reader = PdfReader(pdf_file)
    writer = PdfWriter()

    avg_page_size = (pdf_file.getbuffer().nbytes / (1024 * 1024)) / len(reader.pages)

    current_size = 0
    documents = []
    for page in reader.pages:
        current_size += avg_page_size
        writer.add_page(page)
        if current_size > 10:
            bytes_file = io.BytesIO()
            bytes_file.name = f'split_{len(documents)+1}.pdf'
            writer.write(bytes_file)
            bytes_file.seek(0)
            documents.append(bytes_file)
            writer = PdfWriter()
            current_size = 0

    if len(writer.pages) > 0:
        bytes_file = io.BytesIO()
        writer.write(bytes_file)
        documents.append(bytes_file)

    return documents


def update_page_numbers_and_spans(part_result, page_offset, character_offset, paragraph_offset):
    def update_spans(items, character_offset, key):
        for item in items:
            if isinstance(item[key], dict):
                item[key]['offset'] += character_offset
            else:
                for span in item[key]:
                    span['offset'] += character_offset

    def update_paragraphs(paragraphs, page_offset, character_offset):
        for paragraph in paragraphs:
            for region in paragraph['boundingRegions']:
                region['pageNumber'] += page_offset
            update_spans([paragraph], character_offset, key='spans')

    def update_tables(tables, page_offset, character_offset, paragraph_offset):
        for table in tables:
            for region in table['boundingRegions']:
                region['pageNumber'] += page_offset
            for cell in table['cells']:
                for i in range(len(cell.get('elements', []))):
                    pre_para_num = int(cell['elements'][i].split('/')[-1])
                    cell['elements'][i] = f'/paragraphs/{pre_para_num+paragraph_offset}'
            update_spans([table], character_offset, key='spans')

    for page in part_result['analyzeResult']['pages']:
        page['pageNumber'] += page_offset

        update_spans(page['words'], character_offset, key='span')
        update_spans(page['lines'], character_offset, key='spans')
        update_spans([page], character_offset, key='spans')

    update_paragraphs(part_result['analyzeResult']['paragraphs'], page_offset, character_offset)
    update_tables(part_result['analyzeResult'].get('tables', []), page_offset, character_offset, paragraph_offset)


def merge_adi_results(parts_results):
    results = {}
    for part_n, part_result in enumerate(parts_results):
        if part_n == 0:
            results = part_result
        else:
            page_offset = len(results['analyzeResult']['pages'])
            character_offset = len(results['analyzeResult']['content'])
            paragraph_offset = len(results['analyzeResult']['paragraphs'])

            update_page_numbers_and_spans(part_result, page_offset, character_offset, paragraph_offset)

            results['analyzeResult']['paragraphs'].extend(part_result['analyzeResult']['paragraphs'])
            results['analyzeResult'].setdefault('tables', []).extend(part_result['analyzeResult'].get('tables', []))

            results['analyzeResult']['content'] += '\n' + part_result['analyzeResult']['content']
            results['analyzeResult']['pages'].extend(part_result['analyzeResult']['pages'])

    return results


def split_pdf(original_pdf: BytesIO, parts: int) -> list[BytesIO]:
    """
    Splits a PDF file into multiple smaller parts.
    Args:
        original_pdf (BytesIO): A file-like object representing the original PDF file.
            The object must have a `name` attribute for naming the parts.
        parts (int): The number of parts to split the PDF into.
    Returns:
        list[BytesIO]: A list of file-like objects, each representing a part of the original PDF.
            Each part will have a `name` attribute indicating its filename.
    Notes:
        - The function evenly splits the pages of the original PDF into the specified number of parts.
    """
    # Open original PDF
    original_pdf.seek(0)
    try:
        doc = pymupdf.open(stream=original_pdf)
    except:
        doc = pymupdf.open(stream=original_pdf.read())
    file_name = os.path.basename(original_pdf.name)

    # Calculate the number of pages per part
    total_pages = doc.page_count
    pages_per_part = total_pages // parts

    # Create the new documents
    pdf_parts = []
    for part in range(parts):
        start_page = part * pages_per_part
        end_page = (part + 1) * pages_per_part if part < parts - 1 else total_pages

        # Create a new PDF document
        new_doc = fitz.open()
        for page_num in range(start_page, end_page):
            new_doc.insert_pdf(doc, from_page=page_num, to_page=page_num)
        new_pdf = BytesIO(new_doc.tobytes())
        new_pdf.name = f'part_{part + 1}_{file_name}'
        pdf_parts.append(new_pdf)

    return pdf_parts
