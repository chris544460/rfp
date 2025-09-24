import pandas as pd
import json, os
import re
from typing import List, Dict, Optional, Union
import docx
from docx.text.paragraph import Paragraph
from docx.table import Table


def iter_block_items(parent):
    """
    A generator that yields paragraphs and tables in the order they appear in the DOCX.
    Adapted from python-docx FAQ to preserve reading order.
    """
    if hasattr(parent, "element"):
        elm = parent.element.body if hasattr(parent.element, "body") else parent.element
    else:
        elm = parent
    for child in elm:
        if child.tag.endswith("}p"):
            yield Paragraph(child, parent)
        elif child.tag.endswith("}tbl"):
            yield Table(child, parent)


class ExcelQuestionnaireParser:
    """
    Parses two-column questionnaire XLSX into JSON records.
    """
    def __init__(self, file_path: str, sheet_name: Optional[Union[str,int]] = 0, section: Optional[str] = None):
        self.file_path = file_path
        self.sheet_name = sheet_name
        self.section = section
        self.records: List[Dict[str,Optional[str]]] = []

    def parse(self) -> List[Dict[str,Optional[str]]]:
        df = pd.read_excel(self.file_path, sheet_name=self.sheet_name, header=None)
        n = len(df)
        i = 0
        while i < n:
            key = df.iloc[i,0]
            txt = df.iloc[i,1]
            if pd.notna(key) and pd.notna(txt):
                q = str(txt).strip()
                ans_parts: List[str] = []
                i += 1
                while i < n and pd.isna(df.iloc[i,0]):
                    a = df.iloc[i,1]
                    if pd.notna(a): ans_parts.append(str(a).strip())
                    i += 1
                self.records.append({"source":self.file_path, "section":self.section,
                                     "field":q, "value":"\n".join(ans_parts)})
            else:
                i += 1
        return self.records

    def to_json(self, output_path: str) -> None:
        if not self.records: self.parse()
        with open(output_path,'w',encoding='utf-8') as f:
            json.dump(self.records,f,indent=2,ensure_ascii=False)


class ExcelAnswerLibraryParser:
    """
    Parses Answer Library XLSX into JSON.
    Expects 'ID','Question','Answer_Response*', plus metadata.
    """
    def __init__(self, file_path: str, sheet_name: Optional[Union[str,int]]=0):
        self.file_path = file_path
        self.sheet_name = sheet_name
        self.records: List[Dict[str,Optional[Union[str,List[str]]]]] = []

    def parse(self) -> List[Dict[str,Optional[Union[str,List[str]]]]]:
        df = pd.read_excel(self.file_path, sheet_name=self.sheet_name)
        answer_cols = [c for c in df.columns if str(c).startswith('Answer_Response')]
        for _,row in df.iterrows():
            base = {'source':self.file_path, 'id':str(row.get('ID','')).strip(),
                    'question':str(row.get('Question','')).strip(),
                    'alternate_questions':[], 'answers':[]}
            alt = row.get('Alternate Questions')
            if pd.notna(alt):
                base['alternate_questions'] = [a.strip() for a in str(alt).split(';') if a.strip()]
            for col in answer_cols:
                v = row.get(col)
                if pd.notna(v) and str(v).strip(): base['answers'].append(str(v).strip())
            yn = row.get('Answer_No/Yes')
            if pd.notna(yn): base['yes_no'] = str(yn).strip()
            sec = row.get('Section Name')
            if pd.notna(sec): base['section'] = str(sec).strip()
            tags = row.get('Tags')
            if pd.notna(tags): base['tags'] = [t.strip() for t in str(tags).split(';') if t.strip()]
            self.records.append(base)
        return self.records

    def to_json(self, output_path: str) -> None:
        if not self.records: self.parse()
        with open(output_path,'w',encoding='utf-8') as f:
            json.dump(self.records,f,indent=2,ensure_ascii=False)


class MixedDocParser:
    """
    DOCX parser capturing headings, paragraphs, 2-col Q&A, multi-col tables.
    """
    HEADING_PATTERN = re.compile(r'^(\d+(?:\.\d+)+)\s+.*')

    def __init__(self,file_path:str):
        self.file_path = file_path
        self.records:List[Dict] = []
        self.current_section = 'Document'

    def parse(self)->List[Dict]:
        doc = docx.Document(self.file_path)
        for block in iter_block_items(doc):
            if isinstance(block,Paragraph): self._handle_paragraph(block)
            elif isinstance(block,Table): self._handle_table(block)
        return self.records

    def _handle_paragraph(self,p:Paragraph):
        txt = p.text.strip()
        if not txt: return
        style = p.style.name.lower() if p.style else ''
        if style.startswith('heading'):
            self.current_section = txt
            self.records.append({'source':self.file_path,'type':'heading','section':txt})
        else:
            m = self.HEADING_PATTERN.match(txt)
            if m:
                self.current_section = txt
                self.records.append({'source':self.file_path,'type':'heading','section':txt})
            else:
                self.records.append({'source':self.file_path,'type':'paragraph',
                                     'section':self.current_section,'text':txt})

    def _handle_table(self,tbl:Table):
        n = len(tbl.columns)
        if n==2: self._parse_2col_qa(tbl)
        else: self._parse_multi_col(tbl)

    def _parse_2col_qa(self,tbl:Table):
        cur_q=None; ans_parts:List[str]=[]
        def flush():
            nonlocal cur_q,ans_parts
            if cur_q is not None:
                self.records.append({'source':self.file_path,'type':'table_qa',
                    'section':self.current_section,'field':cur_q,
                    'value':'\n'.join(ans_parts).strip()})
        for row in tbl.rows:
            c0=row.cells[0].text.strip(); c1=row.cells[1].text.strip()
            if c0 and c1:
                flush(); cur_q,ans_parts=c0,[c1]
            elif not c0 and c1 and cur_q is not None:
                ans_parts.append(c1)
            else:
                if c0: flush(); cur_q,ans_parts=c0,[]
        flush()

    def _parse_multi_col(self,tbl:Table):
        rows=[[c.text.strip() for c in r.cells] for r in tbl.rows]
        if not rows: return
        hdr=rows[0]
        for i,row in enumerate(rows[1:],start=1):
            rec={hdr[j]:row[j] if j<len(row) else '' for j in range(len(hdr))}
            self.records.append({'source':self.file_path,'type':'table_data',
                                'section':self.current_section,'row_index':i,'data':rec})

    def to_json(self,out:str)->None:
        if not self.records: self.parse()
        with open(out,'w',encoding='utf-8') as f:
            json.dump(self.records,f,indent=2,ensure_ascii=False)


class LoopioExcelParser:
    """
    Converts Loopio XLSX to Responsive JSON format.
    """
    def __init__(self,file_path:str):
        self.file_path=file_path
        self.records:List[Dict]=[]

    def parse(self)->List[Dict]:
        df=pd.read_excel(self.file_path,engine='openpyxl')
        df.columns=[c.lower().strip() if isinstance(c,str) else c for c in df.columns]
        for _,r in df.iterrows():
            q=str(r.get('question *','')).strip()
            a=str(r.get('answer *','')).strip()
            if not q or not a: continue
            ans=[x.strip() for x in a.split(';') if x.strip()] or [a]
            tags=[t.strip() for t in str(r.get('stack','')).split(',') if t.strip()]
            cat=str(r.get('category','')).strip(); sub=str(r.get('sub-category','')).strip()
            if cat and sub: sec=f"{cat} > {sub}"
            elif cat: sec=cat
            else: sec='General'
            alts=[]
            for i in range(1,6):
                col=f'alternate question {i}'
                v=str(r.get(col,'')).strip()
                if v and v.lower()!='nan': alts.append(v)
            rec={'id':str(r.get('library entry id',f"loopio_{len(self.records)}")).strip(),
                 'question':q,'answers':ans,'section':sec,'tags':tags,
                 'source':os.path.basename(self.file_path),'alternate_questions':alts}
            self.records.append(rec)
        return self.records

    def to_json(self,out:str)->None:
        if not self.records: self.parse()
        with open(out,'w',encoding='utf-8') as f:
            json.dump(self.records,f,indent=2,ensure_ascii=False)


def detect_and_parse_excel_file(file_path:str,output_dir:str='./parsed_json_outputs')->bool:
    """
    Auto-detects Loopio vs standard XLSX, outputs JSON. Returns True if Loopio.
    """
    os.makedirs(output_dir,exist_ok=True)
    try:
        samp=pd.read_excel(file_path,engine='openpyxl',nrows=5)
        cols=[str(c).lower().strip() for c in samp.columns]
        if all(ic in cols for ic in ['library entry id','question *','answer *']):
            print(f"Detected Loopio format: {file_path}")
            recs=LoopioExcelParser(file_path).parse()
            out=os.path.join(output_dir,f"{os.path.splitext(os.path.basename(file_path))[0]}.json")
            with open(out,'w',encoding='utf-8') as f: json.dump(recs,f,indent=2,ensure_ascii=False)
            print(f"✅ Created {len(recs)} Loopio records in {out}")
            return True
        else:
            print(f"File appears standard format: {file_path}")
            return False
    except Exception as e:
        print(f"❌ Error processing {file_path}: {e}")
        return False


def process_standard_excel_file(file_path:str)->bool:
    """
    Placeholder for existing custom XLSX parsers.
    """
    print(f"Processing standard Excel file: {file_path}")
    # Insert original logic here (e.g. ExcelQuestionnaireParser, ExcelAnswerLibraryParser)
    return True


def process_excel_file_with_detection(file_path:str)->bool:
    """
    Wrapper to process XLSX with auto-detection.
    Returns True if handled by Loopio, False if fallback.
    """
    if detect_and_parse_excel_file(file_path):
        return True
    else:
        return process_standard_excel_file(file_path)


if __name__ == '__main__':
    # Example usage:
    # DOCX parsing
    # doc_path='/path/to/file.docx'
    # parser=MixedDocParser(doc_path)
    # records=parser.parse()
    # for r in records[:10]: print(r)
    # parser.to_json('out.json')

    # Auto-process Excel
    # files=['AIP Q&As Backup.xlsx','Answer LibraryBUKPF-09-05.xlsx']
    # for f in files: process_excel_file_with_detection(f)
    pass
