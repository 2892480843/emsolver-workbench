#!/usr/bin/env python3
"""Render the Chinese submission brief from one shared JSON content source.

The font is supplied explicitly to keep large licensed font assets out of Git.
ReportLab embeds only the glyph subset used by this document. Markdown and PDF
share the same blocks so scope and incomplete-feature statements stay aligned.
"""
from __future__ import annotations
import argparse
from html import escape
import json
from pathlib import Path


def markdown(document):
    lines=['# '+document['title']+' 作品方案介绍','',document['subtitle'].replace('\n',' '),'',
           document['category'],'',document['track'],'',document['version'],'',
           '[完整代码仓库]('+document['repository']+')','']
    for block in document['blocks']:
        kind=block['type'];text=block.get('text','')
        if kind=='pagebreak':continue
        if kind in ('h1','h2'):lines+=['#'*(2 if kind=='h1' else 3)+' '+text,'']
        elif kind in ('body','formula'):lines+=[text,'']
        elif kind=='bullet':lines+=['- '+text,'']
        elif kind=='code':lines+=['```bash',text,'```','']
        elif kind=='table':
            lines+=['| '+' | '.join(block['headers'])+' |','| '+' | '.join('---' for _ in block['headers'])+' |']
            lines+=['| '+' | '.join(str(x).replace('|','\\|').replace('\n','<br>') for x in row)+' |' for row in block['rows']];lines+=['']
        elif kind=='figure':lines+=['!['+block['caption']+']('+block['path']+')','']
        elif kind=='architecture':lines+=['```mermaid','flowchart TB','  Browser[React 工作台] --> API[FastAPI 控制面]','  API --> PG[(PostgreSQL 元数据与租约)]','  API --> Files[(受控文件目录)]','  A[节点 A] <-->|鉴权 HTTP/HTTPS| API','  B[节点 B] <-->|鉴权 HTTP/HTTPS| API','  A --> SolverA[独立数值进程]','  B --> SolverB[独立数值进程]','```','']
    return '\n'.join(lines)


def render(document, root, output, font_path):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (SimpleDocTemplate,Paragraph,Spacer,PageBreak,Table,TableStyle,Image,Flowable,KeepTogether)
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.colors import HexColor,white
    from reportlab.lib.enums import TA_LEFT,TA_CENTER
    from reportlab.lib.units import mm
    pdfmetrics.registerFont(TTFont('Document',str(font_path)))
    pdfmetrics.registerFontFamily('Document',normal='Document',bold='Document',italic='Document',boldItalic='Document')
    ink=HexColor('#191C20');muted=HexColor('#5C6670');accent=HexColor('#2A4A6B');tint=HexColor('#F3F5F7');rule=HexColor('#CAD0D5')
    width,height=A4;margin=26*mm;usable=width-2*margin
    styles={
        'body':ParagraphStyle('body',fontName='Document',fontSize=10.1,leading=16.4,textColor=ink,spaceAfter=10,wordWrap='CJK'),
        'h1':ParagraphStyle('h1',fontName='Document',fontSize=20,leading=29,textColor=ink,spaceAfter=18,keepWithNext=True,wordWrap='CJK'),
        'h2':ParagraphStyle('h2',fontName='Document',fontSize=12.5,leading=19,textColor=ink,spaceBefore=9,spaceAfter=5,keepWithNext=True,wordWrap='CJK'),
        'cell':ParagraphStyle('cell',fontName='Document',fontSize=8.9,leading=14.4,textColor=ink,wordWrap='CJK'),
        'th':ParagraphStyle('th',fontName='Document',fontSize=9,leading=14,textColor=white,wordWrap='CJK'),
        'caption':ParagraphStyle('caption',fontName='Document',fontSize=8,leading=12,textColor=muted,spaceBefore=6,spaceAfter=12,wordWrap='CJK'),
        'code':ParagraphStyle('code',fontName='Courier',fontSize=8.1,leading=12,textColor=ink,backColor=tint,borderPadding=10,spaceBefore=5,spaceAfter=12),
        'formula':ParagraphStyle('formula',fontName='Document',fontSize=11.2,leading=20,textColor=ink,alignment=TA_CENTER,spaceBefore=12,spaceAfter=18,wordWrap='CJK'),
    }
    def para(text,style='body'):return Paragraph(escape(str(text)).replace('\n','<br/>'),styles[style])
    class Architecture(Flowable):
        def __init__(self):super().__init__();self.width=usable;self.height=218
        def draw(self):
            c=self.canv;c.saveState();c.setStrokeColor(rule);c.setLineWidth(.7)
            def box(x,y,w,h,title,sub=''):
                c.setFillColor(tint);c.rect(x,y,w,h,fill=1,stroke=1);c.setFillColor(ink);c.setFont('Document',10);c.drawCentredString(x+w/2,y+h/2+(3 if sub else -3),title)
                if sub:c.setFont('Document',7.5);c.setFillColor(muted);c.drawCentredString(x+w/2,y+9,sub)
            def arrow(x1,y1,x2,y2):
                from math import atan2,cos,sin
                c.setStrokeColor(accent);c.line(x1,y1,x2,y2);a=atan2(y2-y1,x2-x1);c.line(x2,y2,x2-5*cos(a-.4),y2-5*sin(a-.4));c.line(x2,y2,x2-5*cos(a+.4),y2-5*sin(a+.4))
            col=(usable-32)/3;xs=[0,col+16,2*(col+16)]
            box(xs[0],158,col,44,'浏览器工作台','身份与项目角色');box(xs[1],158,col,44,'FastAPI 控制面','计划 / 状态 / 独立复验');box(xs[2],158,col,44,'PostgreSQL','实体 / 事件 / 持久租约')
            arrow(xs[0]+col,180,xs[1],180);arrow(xs[1]+col,180,xs[2],180)
            box(xs[0],62,col,44,'受控文件目录','输入 / 规范结果');box(xs[1],62,col,44,'计算节点 A','独立工作目录');box(xs[2],62,col,44,'计算节点 B','独立工作目录')
            arrow(xs[1]+col/2,158,xs[0]+col/2,106);arrow(xs[1]+col/2,158,xs[1]+col/2,106);arrow(xs[1]+col/2,158,xs[2]+col/2,106)
            c.setFillColor(muted);c.setFont('Document',7.5);c.drawString(xs[1]+col/2+5,130,'鉴权 HTTP / HTTPS')
            box(xs[1],0,col,36,'数值子进程');box(xs[2],0,col,36,'数值子进程');arrow(xs[1]+col/2,62,xs[1]+col/2,36);arrow(xs[2]+col/2,62,xs[2]+col/2,36)
            c.restoreState()
    def draw_page(canvas,doc):
        canvas.saveState();canvas.setTitle('EMSolver 作品方案介绍');canvas.setAuthor('EMSolver contributors');canvas.setSubject('中国商飞06赛题：电磁谐振与微波器件求解方案')
        if doc.page==1:
            canvas.setFillColor(accent);canvas.rect(margin,height-106*mm,4,57*mm,fill=1,stroke=0)
            y=height-40*mm;canvas.setFont('Document',10);canvas.setFillColor(muted);canvas.drawString(margin,y,document['category'])
            canvas.setFont('Document',43);canvas.setFillColor(ink);canvas.drawString(margin+17,y-79,document['title'])
            y-=117
            for line in document['subtitle'].split('\n'):
                canvas.setFont('Document',19);canvas.drawString(margin+17,y,line);y-=32
            canvas.setStrokeColor(accent);canvas.setLineWidth(1);canvas.line(margin,y-18,width-margin,y-18)
            canvas.setFont('Document',11);canvas.setFillColor(muted);canvas.drawString(margin,y-57,document['track']);canvas.drawString(margin,y-82,document['version'])
            summary=para(document['abstract']);_,h=summary.wrap(usable,80);summary.drawOn(canvas,margin,242-h)
            canvas.setFont('Document',9);canvas.setFillColor(ink);canvas.drawString(margin,168,'项目背景 · 技术架构 · 应用场景 · 创新点 · 功能完成度')
            canvas.setFont('Document',8);canvas.setFillColor(muted);canvas.drawString(margin,117,document['repository']);canvas.linkURL(document['repository'],(margin,112,width-margin,127),relative=0)
            canvas.setFont('Document',8);canvas.drawString(margin,93,'提供完整源码与复现入口；未闭环功能和验证边界在正文明确说明。')
        else:
            canvas.setFont('Document',7.7);canvas.setFillColor(muted);canvas.drawString(margin,height-17*mm,'EMSolver / 作品方案介绍');canvas.drawRightString(width-margin,height-17*mm,'AI+ 工业软件 · 中国商飞 06')
            canvas.setStrokeColor(accent);canvas.setLineWidth(.8);canvas.line(margin,height-20*mm,width-margin,height-20*mm)
        canvas.setStrokeColor(rule);canvas.setLineWidth(.5);canvas.line(margin,17*mm,width-margin,17*mm)
        canvas.setFont('Document',7.3);canvas.setFillColor(muted);canvas.drawString(margin,12*mm,'2026-10-16  |  参赛源码与证据快照');canvas.drawRightString(width-margin,12*mm,str(doc.page));canvas.restoreState()
    story=[Spacer(1,620),PageBreak()]
    for block in document['blocks']:
        kind=block['type'];text=block.get('text','')
        if kind=='pagebreak':story.append(PageBreak())
        elif kind in ('body','h1','h2'):story.append(para(text,kind))
        elif kind=='formula':
            story.append(Paragraph(escape(text).replace('_i','<sub>i</sub>').replace('_2','<sub>2</sub>'),styles['formula']))
        elif kind=='bullet':story.append(para('• '+text))
        elif kind=='code':story.append(Paragraph(escape(text).replace(' ','&#160;').replace('\n','<br/>'),styles['code']))
        elif kind=='table':
            data=[[para(x,'th') for x in block['headers']]]+[[para(x,'cell') for x in row] for row in block['rows']]
            fractions=block.get('col_widths',[1/len(data[0])]*len(data[0]));t=Table(data,colWidths=[usable*f for f in fractions],repeatRows=1,hAlign='LEFT')
            padding=4 if block.get('compact') else 6
            t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),accent),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),padding),('BOTTOMPADDING',(0,0),(-1,-1),padding),('ROWBACKGROUNDS',(0,1),(-1,-1),[white,tint]),('LINEBELOW',(0,-1),(-1,-1),.4,rule)]));story.extend([t,Spacer(1,12)])
        elif kind=='architecture':story.extend([Architecture(),Spacer(1,15)])
        elif kind=='figure':
            from PIL import Image as PILImage
            path=root/block['path']
            with PILImage.open(path) as image:w,h=image.size
            scale=min(usable/w,block.get('max_height',320)/h)
            story.append(KeepTogether([Image(str(path),width=w*scale,height=h*scale),para(block['caption'],'caption')]))
    output.parent.mkdir(parents=True,exist_ok=True)
    doc=SimpleDocTemplate(str(output),pagesize=A4,leftMargin=margin,rightMargin=margin,topMargin=28*mm,bottomMargin=25*mm,title='EMSolver 作品方案介绍')
    doc.build(story,onFirstPage=draw_page,onLaterPages=draw_page)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--content',type=Path,default=Path('docs/submission/project-introduction.json'))
    parser.add_argument('--font',type=Path,required=True,help='Licensed TrueType font with Chinese glyphs')
    parser.add_argument('--output',type=Path,default=Path('output/pdf/EMSolver-project-introduction.pdf'))
    args=parser.parse_args(argv)
    document=json.loads(args.content.read_text(encoding='utf-8'))
    args.content.with_name('作品方案介绍.md').write_text(markdown(document),encoding='utf-8')
    render(document,args.content.parent,args.output,args.font)
    print('PDF written:',args.output)


if __name__=='__main__':main()
