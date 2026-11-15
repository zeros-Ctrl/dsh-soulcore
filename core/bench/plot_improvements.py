"""Plot aggregate CSVs. Requires matplotlib, pandas, numpy; no network or model API.

python -X utf8 -B core/bench/plot_improvements.py DATA_DIR FIGURE_DIR [--qa-scripts SKILL_SCRIPTS]
SVG text stays editable; PNGs are exported at 300 dpi. No observations are filtered
for outcomes. Missing old arousal and non-restart errors are structural, documented.
"""
import argparse
import json
from pathlib import Path
import sys
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
import numpy as np
import pandas as pd


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('data',type=Path);parser.add_argument('out',type=Path)
    parser.add_argument('--qa-scripts',type=Path)
    args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
    assert (args.data/'data-audit.json').is_file(), 'Run data analysis first'
    fonts={f.name for f in font_manager.fontManager.ttflist}
    selected=next((f for f in ['Microsoft YaHei','Noto Sans CJK SC','SimHei'] if f in fonts),None)
    if not selected:raise RuntimeError('Install an available Chinese font before drawing Chinese labels')
    plt.rcParams.update({'font.family':'sans-serif','font.sans-serif':[selected,'DejaVu Sans'],
                         'axes.unicode_minus':False,'svg.fonttype':'none','font.size':10,
                         'axes.spines.top':False,'axes.spines.right':False,
                         'axes.titleweight':'bold','axes.grid':True,'grid.alpha':.15,
                         'figure.facecolor':'white','savefig.facecolor':'white'})
    colors={'before':'#657381','after':'#0072B2','surfcore':'#D55E00'}
    labels={'before':'SoulCore 旧版','after':'SoulCore 新版','surfcore':'SurfCore 纯向量模块'}
    audit=[]
    qa=None
    if args.qa_scripts:
        sys.path.insert(0,str(args.qa_scripts.resolve()))
        from visual_qa import audit_layout
        qa=audit_layout
    def save(fig,name,title,caption):
        fig.suptitle(title,fontsize=16,y=.98)
        fig.text(.03,.025,caption,fontsize=9,color='#46515C',va='bottom')
        fig.tight_layout(rect=(0,.09,1,.94))
        if qa:audit.append({'figure':name,'issues':qa(fig)})
        fig.savefig(args.out/(name+'.svg'))
        vector=args.out/(name+'.svg')
        vector.write_text('\n'.join(line.rstrip() for line in vector.read_text(encoding='utf-8').splitlines())+'\n',
                          encoding='utf-8',newline='\n')
        fig.savefig(args.out/(name+'.png'),dpi=300)
        plt.close(fig)
    read=lambda name:pd.read_csv(args.data/name)
    retrieval=read('retrieval.csv');isolation=read('project-isolation.csv')
    categories=['lexical','paraphrase','cross','heldout','heldout_hard','blind','all']
    category_names=['字面问题','改述问题','跨概念问题','已有留出题','困难留出题','已有盲题','全部题目']
    fig,axes=plt.subplots(1,2,figsize=(12,5.8),gridspec_kw={'width_ratios':[1.35,1]})
    for j,version in enumerate(['before','after','surfcore']):
        rows=retrieval[retrieval.version==version].set_index('category')
        if rows.empty:continue
        axes[0].scatter([rows.loc[c,'recall_at_5']*100 for c in categories],
                        np.arange(len(categories))+(j-1)*.19,s=48,label=labels[version],color=colors[version],marker=['o','s','^'][j])
    counts=retrieval[retrieval.version=='after'].set_index('category').n
    axes[0].set_yticks(range(len(categories)),[f'{name} (n={counts[c]})' for name,c in zip(category_names,categories)])
    axes[0].invert_yaxis();axes[0].set_xlim(-3,104);axes[0].set_xlabel('Recall@5（%）')
    axes[0].set_title('A  同语料、同词法向量的检索');axes[0].legend(loc='lower left',fontsize=9)
    for j,version in enumerate(['before','after']):
        rows=isolation[isolation.version==version]
        values=[rows.own_visible.mean()*100,rows.other_project_visible.mean()*100]
        axes[1].scatter(values,np.arange(2)+(j-.5)*.18,color=colors[version],s=70,marker=['o','s'][j],label=labels[version])
        for value,y in zip(values,np.arange(2)+(j-.5)*.18):
            axes[1].annotate(f'{value:.0f}%',(value,y),xytext=(0,10),textcoords='offset points',ha='center',fontsize=10)
    axes[1].set_yticks([0,1],['本项目可见（越高越好）','其他项目可见（越低越好）'])
    axes[1].invert_yaxis();axes[1].set_xlim(-5,105);axes[1].set_ylim(1.65,-.65)
    axes[1].set_xlabel('20 次合成查询中的比例（%）');axes[1].set_title('B  同名项目归属')
    save(fig,'retrieval-and-projects','检索能力保持，项目归属改善',
         '离线合成题库 / hash512 词法向量；跨概念题仅 3 条。SurfCore 的项目过滤由外部适配器提供。\n同名项目为重复构造场景；这些结果不能外推为其他开源项目的全面排名。')

    longmem=read('longmem.csv');summary=json.loads((args.data/'summary.json').read_text(encoding='utf-8'))
    fig,axes=plt.subplots(1,2,figsize=(12,5.5))
    for ax,key,factor,title,ylabel in zip(axes,['extract_in_chars','inject_chars_mean'],[10000,1],
                                        ['A  200 天提炼输入','B  探测问题的平均上下文注入'],['输入字符（万）','每次注入字符']):
        wide=longmem.pivot(index='seed',columns='version',values=key)
        for j,(seed,row) in enumerate(wide.iterrows()):
            delta=(j-2)*.025
            ax.plot([delta,1+delta],[row['before']/factor,row['after']/factor],color='#ADB7C0',lw=1,alpha=.8)
            ax.scatter([delta,1+delta],[row['before']/factor,row['after']/factor],
                       c=[colors['before'],colors['after']],s=45,zorder=3)
        target=None
        spacing=wide.to_numpy().max()/factor*.031
        for seed,row in wide.sort_values('after').iterrows():
            y=row['after']/factor
            target=y if target is None else max(y,target+spacing)
            j=list(wide.index).index(seed)
            ax.annotate(str(seed),(1+(j-2)*.025,y),xytext=(1.22,target),textcoords='data',
                        fontsize=8,va='center',arrowprops={'arrowstyle':'-','color':'#ADB7C0','lw':.7})
        old=wide.before.mean()/factor;new=wide.after.mean()/factor
        ax.text(.03,.95,f'均值 {old:.1f} → {new:.1f}\n变化 {(new/old-1)*100:+.1f}%',
                transform=ax.transAxes,va='top',fontsize=11)
        ax.set_xticks([0,1],['旧版','新版']);ax.set_xlim(-.2,1.4)
        ax.set_ylim(0,wide.to_numpy().max()/factor*1.2);ax.set_ylabel(ylabel);ax.set_title(title)
    save(fig,'longmem-cost','长期模拟：提炼与注入输入增加',
         f'全部 {len(summary["seeds"])} 组 seed 配对原始点（线旁为 seed）；每版模拟 200 天，不挑选较好的运行。\n字符是成本代理，未调用付费模型；不能把局部画像省调用换算成整体费用下降。')

    emotion=read('emotion.csv');policies=read('expression-policy.csv')
    fig,axes=plt.subplots(2,2,figsize=(12,8.5))
    for version in ['before','after']:
        rows=emotion[emotion.version==version].sort_values('minute')
        for seed,group in rows.groupby('seed'):
            axes[0,0].plot(group.minute/60,group.mood,color=colors[version],lw=.6,alpha=.25)
        mean=rows.groupby('minute').mood.mean()
        axes[0,0].plot(mean.index/60,mean.values,color=colors[version],lw=2,
                       ls='--' if version=='before' else '-',label=labels[version])
    axes[0,0].set_title('A  情绪效价：平滑继承旧 PID');axes[0,0].set_ylabel('mood（0–1）')
    axes[0,0].set_xlabel('模拟时间（小时）');axes[0,0].set_ylim(0,1);axes[0,0].legend(fontsize=8)
    for seed,rows in emotion[emotion.version=='after'].groupby('seed'):
        rows=rows.sort_values('minute');axes[0,1].plot(rows.minute/60,rows.arousal,color=colors['after'],lw=.9,alpha=.5)
    axes[0,1].set_title('B  新增独立唤醒度');axes[0,1].set_xlabel('模拟时间（小时）')
    axes[0,1].set_ylabel('arousal（0–1）');axes[0,1].set_ylim(0,1)
    axes[0,1].text(.5,.88,'0 小时夸奖；2 小时批评\n关闭孤独驱动，观察恢复',ha='center',transform=axes[0,1].transAxes,fontsize=9)
    for arousal,style in [(.35,'-'),(.7,'--')]:
        rows=policies[(policies.arousal==arousal)&(~policies.task)]
        axes[1,0].plot(rows.mood,rows.max_tokens,marker='o',ls=style,color=colors['after'],label=f'轻闲聊，唤醒度 {arousal}')
    tasks=policies[(policies.arousal==.35)&policies.task]
    axes[1,0].plot(tasks.mood,tasks.max_tokens,color='#009E73',ls=':',lw=2,label='任务 / 直接问题')
    axes[1,0].set_title('C  表达预算随状态调整');axes[1,0].set_xlabel('情绪效价 mood')
    axes[1,0].set_ylabel('回复 token 上限（策略值）');axes[1,0].set_ylim(0,460);axes[1,0].legend(fontsize=8,loc='lower right')
    restart=emotion.dropna(subset=['restart_mood_error'])
    for j,version in enumerate(['before','after']):
        rows=restart[restart.version==version].sort_values('seed')
        axes[1,1].scatter(np.arange(len(rows))+(j-.5)*.12,rows.restart_mood_error,
                          marker=['o','s'][j],color=colors[version],s=50,label=labels[version])
    axes[1,1].set_xticks(range(len(summary['seeds'])),[str(s) for s in sorted(summary['seeds'])])
    axes[1,1].set_title('D  重启后的 mood 误差');axes[1,1].set_xlabel('seed')
    axes[1,1].set_ylabel('与连续进程的绝对误差');axes[1,1].set_ylim(bottom=-.003);axes[1,1].legend(fontsize=8)
    save(fig,'affect-and-expression','人格改进：状态可持续，表达受情境约束',
         '薄线为全部 seed，粗线为均值；旧版未持久化重启后的随机状态，误差仅代表本轮观测。\n回复上限是本地策略值，尚未测真人感、实际消息量或真实模型 token；任务保留完整预算。')

    budget=read('budgets.csv');current=budget[budget.version=='after'];positive=current[current.configured_budget>0]
    disabled=current[current.configured_budget==0].iloc[0]
    fig,axes=plt.subplots(1,2,figsize=(12,5.5))
    axes[0].plot(positive.configured_budget,positive.answer_record_visible*100,'o-',color=colors['after'],label='完整答案记录可见')
    axes[0].plot(positive.configured_budget,positive.critical_complete_fraction*100,'s:',color='#009E73',label='关键记录完整')
    axes[0].axhline(disabled.answer_record_visible*100,color=colors['before'],ls='--',label='关闭预算 / 旧版固定装配')
    axes[0].set_ylim(0,108);axes[0].set_title('A  过低预算损害普通记录可见率')
    axes[0].set_ylabel('记录可见 / 完整比例（%）');axes[0].legend(loc='lower right',fontsize=9)
    axes[0].annotate(f'{positive.iloc[0].answer_record_visible*100:.1f}%',
                     (positive.iloc[0].configured_budget,positive.iloc[0].answer_record_visible*100),
                     xytext=(14,-14),textcoords='offset points',fontsize=11)
    axes[1].plot(positive.configured_budget,positive.estimated_tokens_mean,'o-',color=colors['after'],label='新版装配结果')
    axes[1].axhline(disabled.estimated_tokens_mean,color=colors['before'],ls='--',label='关闭预算 / 旧版固定装配')
    axes[1].set_ylim(0,240);axes[1].set_title('B  软预算允许关键条目溢出')
    axes[1].set_ylabel('平均上下文长度（估算 token）');axes[1].legend(loc='lower right',fontsize=9)
    for ax in axes:
        ax.set_xlabel('配置召回预算（估算 token）');ax.set_xticks(positive.configured_budget)
    save(fig,'budget-tradeoff','预算权衡：保留关键记录，不能保证极低预算下完整召回',
         '全部 102 个已有题库问题；图中的关闭参考线包含预算=0 的观测。旧版没有 token 预算接口。\n原文未因装配预算被删除；完整记录可见率不等于模型最终回答正确率。默认预算为 800。')
    (args.out/'figure-qa.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'out':str(args.out),'font':selected,'figures':4,'qa':audit},ensure_ascii=False))


if __name__=='__main__':main()
