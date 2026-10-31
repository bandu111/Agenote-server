"""Private, evidence-bound PDF work product. No remote fonts or raw Markdown."""
from io import BytesIO
from pathlib import Path
from html import escape
import hashlib,json,os,secrets,threading,re
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,KeepTogether,Table,TableStyle
from app.core.config import ROOT

FONT_PATH=ROOT/'assets/fonts/NotoSansSC-Regular.ttf'
_lock=threading.Lock()
BLUE=colors.HexColor('#065CFF');INK=colors.HexColor('#101842');MUTED=colors.HexColor('#7885A6')

def font():
    with _lock:
        if 'AgenoteCJK' not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont('AgenoteCJK',str(FONT_PATH)))
    return 'AgenoteCJK'

def clean(value):
    return escape(str(value or '')).replace('\n','<br/>')

def render_pdf(report, *, version=1, created_at='', profession=''):
    name = font()
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=43, leftMargin=43,
        topMargin=62, bottomMargin=48, title=str(report.get('title') or 'Agenote 工作成果'), author='Agenote')
    styles = {k: ParagraphStyle(k, fontName=name, fontSize=size, leading=leading,
        textColor=color, wordWrap='CJK', spaceAfter=space) for k,size,leading,color,space in [
        ('title',22,31,INK,12), ('heading',13,22,INK,10), ('body',9.5,16,INK,6),
        ('muted',8,13,MUTED,5), ('blue',9,15,BLUE,5), ('cell',8,13,INK,0)]}
    styles['heading'].keepWithNext = True
    def p(value, kind='body'): return Paragraph(clean(value), styles[kind])
    story = []
    def heading(title): story.extend([Spacer(1,14), p(title,'heading')])
    def table(columns, rows, widths=None):
        if not rows: return
        rows = [[p(v,'cell') for v in columns]] + [[p(v,'cell') for v in row] for row in rows]
        item = Table(rows, colWidths=widths or [doc.width/len(columns)]*len(columns), repeatRows=1,
            hAlign='LEFT', splitInRow=1)
        item.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#EAF0FF')),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#F8FAFF')]),
            ('LINEBELOW',(0,0),(-1,0),.6,colors.HexColor('#BDCEFF')),
            ('LINEBELOW',(0,1),(-1,-1),.3,colors.HexColor('#E2E8F5')),
            ('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),9),
            ('RIGHTPADDING',(0,0),(-1,-1),9),('TOPPADDING',(0,0),(-1,-1),9),
            ('BOTTOMPADDING',(0,0),(-1,-1),9)]))
        story.append(item)

    # Stable E labels cover metrics and computation operands as well as prose.
    citations = []
    def collect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == 'evidence_ids' and isinstance(child, list):
                    for ref in child:
                        if ref in (report.get('evidence') or {}) and ref not in citations: citations.append(ref)
                elif key != 'evidence': collect(child)
        elif isinstance(value, list):
            for child in value: collect(child)
    collect(report)
    def refs(keys): return ' '.join('[E'+str(citations.index(k)+1)+']' for k in keys if k in citations)
    def certainty(value): return {'observation':'原文观察','correlation':'相关性·非因果结论',
        'hypothesis':'待验证假设','unverified':'尚无法核验'}.get(value,'尚待核对')

    lens = report.get('role_lens') or {}
    meta = report.get('report_meta') or {}
    profession = profession or lens.get('raw') or lens.get('category') or '通用分析'
    story.extend([p(meta.get('report_type') or '职业定制工作成果','blue'),p(report.get('title'),'title')])
    table(['岗位 / 版本','分析期间','数据覆盖期间'], [[profession+' / V'+str(version),
        meta.get('time_window') or '原始资料未明确', meta.get('data_window') or '以引用资料实际覆盖为准']])
    if created_at: story.append(p('生成时间：'+created_at,'muted'))
    if report.get('completion') == 'partial': story.append(p('部分完成：缺失资料与受阻分析在附录列明。','muted'))
    heading('01  结论先行')
    story.append(p(report.get('summary')))
    table(['核心结论','判断性质','依据'], [[f.get('text',''),certainty(f.get('certainty')),refs(f.get('evidence_ids',[]))]
        for f in report.get('findings',[])], [doc.width*.64, doc.width*.2, doc.width*.16])
    if report.get('analysis_goal'): story.extend([Spacer(1,9),p('本次回答：'+report['analysis_goal'],'muted')])

    heading('02  关键指标看板')
    rows = []
    for metric in report.get('metrics',[]):
        status = metric.get('status')
        rows.append([metric['name'], metric.get('value') if status in ['quoted','computed'] else '待补充',
            '服务器计算' if status == 'computed' else '原文引用·未独立核验' if status == 'quoted' else '缺少数据',
            (metric.get('missing_data') if status == 'missing' else metric.get('quote') or '')+' '+refs(metric.get('evidence_ids',[]))])
    table(['指标','当前数值','数据状态','口径 / 依据'], rows,
        [doc.width*.2, doc.width*.18, doc.width*.23, doc.width*.39])
    heading('03  主体分析')
    for section in report.get('sections',[]):
        story.extend([p(section['title'],'blue'),p(section['content']),p('依据 '+refs(section.get('evidence_ids',[])),'muted'),Spacer(1,7)])
    for item in report.get('analysis_tables',[]):
        story.extend([Spacer(1,9),p(item['title'],'blue')])
        table(item['columns'], [[cell['text']+' '+refs(cell.get('evidence_ids',[])) for cell in row] for row in item['rows']])
        if item.get('interpretation'): story.extend([Spacer(1,7),p(item['interpretation'])])

    heading('04  异常与风险')
    risks = sorted(report.get('risks',[]), key=lambda r: ['high','medium','low'].index(r['severity']))
    if risks:
        table(['级别','风险及影响','判断 / 依据'], [[{'high':'高','medium':'中','low':'低'}[r['severity']],
            r['title']+'\n'+r['detail'],certainty(r['certainty'])+'\n'+refs(r['evidence_ids'])] for r in risks],
            [doc.width*.09,doc.width*.66,doc.width*.25])
    else: story.append(p('未形成有充分依据的独立风险条目。','muted'))

    heading('05  行动建议')
    if report.get('actions'):
        table(['行动及预期结果','负责人 / 期限','性质 / 依据'], [[a['action']+'\n'+(a.get('expected_outcome') or '预期效果待评估'),
            (a.get('owner') or '负责人待明确')+'\n'+(a.get('deadline') or '期限待明确'),
            ('会议明确确认' if a.get('basis')=='meeting_confirmed' else 'AI建议·待确认')+'\n'+refs(a.get('evidence_ids',[]))]
            for a in report['actions']], [doc.width*.54,doc.width*.25,doc.width*.21])
    else:
        for n,step in enumerate(report.get('next_steps',[]),1): story.append(p(f'{n}. 建议·待确认：{step}'))
    story.extend([Spacer(1,6),p('建议不等于会议承诺；本报告不会据此自动创建或修改任务、负责人和截止时间。','muted')])

    heading('06  数据来源、口径与局限性')
    for n,key in enumerate(citations,1):
        source = (report.get('evidence') or {})[key]
        at = source.get('at')
        at_label = f'{int(at)//60000:02d}:{int(at)//1000%60:02d}' if isinstance(at,(int,float)) else ''
        title = source.get('title') or ('会议发言 '+at_label)
        story.extend([p(f'E{n}  {title}','blue'),p(str(source.get('text') or '')[:600],'muted')])
    for result in report.get('calculation_results',[]):
        story.extend([Spacer(1,5),p(result['name']+' = '+result['value'],'blue'),
            p('运算：'+result['operation']+'；舍入：'+result['rounding'],'muted')])
        for operand in result['operands']: story.append(p(operand['value']+operand['unit']+' / '+operand['quote'],'muted'))
        story.append(p(result['limitation'],'muted'))
    for title,items in [('方法与执行范围',[m['name']+'：'+('已执行' if m['status']=='performed' else '受阻')+'；'+m['detail'] for m in report.get('methods',[])]),
            ('统计口径',report.get('methodology_notes',[])),('数据缺口及其影响',report.get('data_gaps',[]))]:
        if items:
            story.extend([Spacer(1,9),p(title,'blue')])
            for item in items: story.append(p('• '+str(item)))
    story.extend([Spacer(1,9),p('引用可追溯不等于来源已独立核验；相关性不代表因果。本报告是生成时可访问资料的版本快照，未承诺自动刷新。','muted')])

    def chrome(canvas, document):
        canvas.saveState(); canvas.setFont('Helvetica-Bold',13); canvas.setFillColor(BLUE)
        canvas.drawString(43,A4[1]-35,'Agenote'); canvas.setStrokeColor(colors.HexColor('#E5EBF7'))
        canvas.line(43,A4[1]-45,A4[0]-43,A4[1]-45)
        canvas.setFont(name,8);canvas.setFillColor(MUTED);canvas.drawString(43,26,'职业定制工作成果 / V'+str(version))
        canvas.drawRightString(A4[0]-43,26,str(document.page));canvas.restoreState()
    doc.build(story,onFirstPage=chrome,onLaterPages=chrome)
    return buffer.getvalue()


def private_pdf(row):
    result=row.content.get('result') or {}
    if row.status!='succeeded' or not result.get('findings'):raise ValueError('report_not_ready')
    digest=hashlib.sha256(json.dumps({'layout':3,'report':result},sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    directory=ROOT/'storage/outcome-pdfs'/row.owner_id/row.id/digest
    if not directory.resolve().is_relative_to((ROOT/'storage/outcome-pdfs').resolve()):raise ValueError('invalid_report_path')
    directory.mkdir(parents=True,exist_ok=True,mode=0o700)
    title=re.sub(r'[^a-zA-Z0-9\u4e00-\u9fff_-]+','-',str(result.get('title') or '工作成果')).strip('-')[:54]
    path=directory/('Agenote-'+title+'-v'+str(row.version)+'.pdf')
    if not path.exists():
        data=render_pdf(result,version=row.version,created_at=row.content.get('finishedAt',''))
        temp=directory/(secrets.token_hex(12)+'.tmp')
        with temp.open('xb') as file:os.chmod(temp,0o600);file.write(data)
        os.replace(temp,path)
    return path
