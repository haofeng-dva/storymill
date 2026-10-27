# -*- coding: utf-8 -*-
"""Build a small, auditable first-three-chapters retention calibration set.

The source workbook is read as XML so this script has no external dependency.
It emits JSONL examples, a calibration rule pack, and a compact report.
"""
import argparse, json, math, os, re, statistics, zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree as ET

NS = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main', 'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}

def norm(s):
    s = str(s or '').replace('\u00a0', ' ').strip().lower()
    s = re.sub(r'[（(][^）)]*[）)]', '', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip(' ._-')

def xlsx_rows(path):
    with zipfile.ZipFile(path) as z:
        ss=[]
        if 'xl/sharedStrings.xml' in z.namelist():
            root=ET.fromstring(z.read('xl/sharedStrings.xml'))
            for si in root.findall('m:si', NS): ss.append(''.join(t.text or '' for t in si.iter('{%s}t' % NS['m'])))
        wb=ET.fromstring(z.read('xl/workbook.xml'))
        rel=ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))
        rels={x.attrib['Id']:x.attrib['Target'] for x in rel}
        sheets=wb.find('m:sheets', NS)
        target=None
        for sh in sheets:
            if sh.attrib['name']=='分天分书数据表': target='xl/'+rels[sh.attrib['{'+NS['r']+'}id']].lstrip('/')
        if not target: raise RuntimeError('分天分书数据表 not found')
        root=ET.fromstring(z.read(target)); out=[]
        for row in root.findall('.//m:sheetData/m:row', NS):
            cells={}
            for c in row.findall('m:c', NS):
                ref=c.attrib.get('r',''); col=re.match(r'([A-Z]+)',ref)
                if not col: continue
                idx=0
                for ch in col.group(1): idx=idx*26+ord(ch)-64
                idx-=1; v=c.find('m:v',NS); val='' if v is None else (ss[int(v.text)] if c.attrib.get('t')=='s' else v.text)
                cells[idx]=val
            out.append([cells.get(i,'') for i in range(22)])
        return out

def parse_num(x):
    try: return float(str(x).replace(',',''))
    except: return 0.0

def extract_chapters(text):
    parts=re.split(r'(?im)(?=^\s*####\s*Chapter\s+\d+\s*####)', text)
    chapters=[]
    for p in parts:
        m=re.search(r'####\s*Chapter\s+(\d+)',p,re.I)
        if m and int(m.group(1))<=3:
            body=re.sub(r'^.*?####\s*Chapter\s+\d+\s*####\s*', '', p, flags=re.I|re.S)
            chapters.append((int(m.group(1)), body.strip()))
    return chapters

def features(chapters):
    first=' '.join(x[1] for x in chapters[:3]); words=re.findall(r"[A-Za-z']+", first)
    dialogue=len(re.findall(r'[“\"]', first))/2
    paras=[p.strip() for p in re.split(r'\n\s*\n', first) if p.strip()]
    endings=' '.join(x[1][-500:] for x in chapters[:3])
    return {'first3_words':len(words), 'chapter_words':[len(re.findall(r"[A-Za-z']+",x[1])) for x in chapters[:3]],
      'dialogue_ratio':round(dialogue/max(1,len(words)),4), 'paragraphs':len(paras),
      'hook_markers':len(re.findall(r'\b(but|until|suddenly|then|tomorrow|secret|truth|dead|gone|phone|door|blood)\b',endings,re.I)),
      'question_marks':first.count('?'), 'exclamation_marks':first.count('!'),
      'starts_in_scene':bool(re.search(r'\b(heard|saw|opened|ran|stood|woke|walked|looked|called|pulled|grabbed|felt)\b', chapters[0][1][:500],re.I)) if chapters else False}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--source',required=True); ap.add_argument('--out',default='distill/retention_v1'); args=ap.parse_args()
    os.makedirs(args.out,exist_ok=True); root=os.path.dirname(args.source); textdir=os.path.join(root,'书籍内容')
    rows=xlsx_rows(args.source); books={}
    for r in rows[1:]:
        title=norm(r[1])
        if not title or title in ('总消耗','vo总消耗','正常消耗','测试消耗'): continue
        b=books.setdefault(title, {'title':str(r[1]),'spend':0,'clicks':0,'views':0,'regs':0,'purchases':0,'revenue':0})
        b['spend']+=parse_num(r[2]); b['clicks']+=parse_num(r[3]); b['views']+=parse_num(r[5]); b['regs']+=parse_num(r[6]); b['purchases']+=parse_num(r[9]); b['revenue']+=parse_num(r[11])
    files={norm(re.sub(r'\(\d+-\d+章\)$','',os.path.splitext(f)[0])):f for f in os.listdir(textdir) if f.lower().endswith('.txt')}
    examples=[]
    for key,b in books.items():
        if key not in files: continue
        with open(os.path.join(textdir,files[key]),encoding='utf-8-sig',errors='ignore') as f: ch=extract_chapters(f.read())
        if len(ch)<3: continue
        b.update({'ctr':b['clicks'] and b['views']/b['clicks'] or 0, 'reg_rate':b['clicks'] and b['regs']/b['clicks'] or 0, 'purchase_rate':b['clicks'] and b['purchases']/b['clicks'] or 0, 'roi':b['spend'] and b['revenue']/b['spend'] or 0})
        b['retention_score']=round(0.45*b['reg_rate']+0.30*b['purchase_rate']*20+0.25*b['roi'],6)
        examples.append({'book':b['title'],'source_file':files[key],'metrics':b,'features':features(ch),'chapters':[{'number':n,'excerpt':t[:1200]} for n,t in ch[:3]]})
    examples.sort(key=lambda x:x['metrics']['retention_score'], reverse=True)
    # Interleave score bands so train/validation are not time- or genre-clustered.
    n_valid=min(14, len(examples)); valid=[]
    for i in range(n_valid):
        e=examples[round(i*(len(examples)-1)/max(1,n_valid-1))]
        if e not in valid: valid.append(e)
    train=[e for e in examples if e not in valid][:40]
    for fn,data in [('train.jsonl',train),('validation.jsonl',valid)]:
        with open(os.path.join(args.out,fn),'w',encoding='utf-8') as f:
            for e in data: f.write(json.dumps(e,ensure_ascii=False)+'\n')
    pool=train+valid; med={k:statistics.median([e['features'][k] for e in pool]) for k in ('dialogue_ratio','hook_markers','first3_words')}
    rules={'version':'retention_v1','scope':'first_three_chapters','derived_from':{'matched_books':len(examples),'train':len(train),'validation':len(valid)},'rules':[
      'Open in-scene within the first 2-3 sentences; reveal context through action rather than exposition.',
      'Place a concrete conflict or irreversible choice in Chapter 1, not only a premise.',
      'Escalate the protagonist\'s personal cost in Chapter 2 with a new fact, obstacle, or betrayal.',
      'End each of the first three chapters with an unresolved question, threat, reveal, or forced decision.',
      'Use dialogue and physical action to carry information; avoid long explanatory backstory blocks.',
      f"Calibration reference: target roughly {med['first3_words']:.0f}+ words across the first three chapters, with recurring concrete hook language and dialogue near the observed median ({med['dialogue_ratio']:.3f})."
    ],'feature_medians':med}
    with open(os.path.join(args.out,'calibration_rules.json'),'w',encoding='utf-8') as f: json.dump(rules,f,ensure_ascii=False,indent=2)
    report={'source':args.source,'matched_books':len(examples),'train':len(train),'validation':len(valid),'all_matched':len(examples),'top_score':examples[0]['metrics']['retention_score'] if examples else None,'median_score':statistics.median([e['metrics']['retention_score'] for e in examples]) if examples else None,'notes':['Rows are book-level aggregates after excluding summary/test rows.','Validation split is deterministic and spread across the ranked score range.','Scores are directional proxies, not causal retention labels.']}
    with open(os.path.join(args.out,'report.json'),'w',encoding='utf-8') as f: json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
