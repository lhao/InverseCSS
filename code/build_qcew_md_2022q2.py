import csv,glob,re,sys,collections
R,O=sys.argv[1],sys.argv[2]
ind={};vint={}
for l in open(R+'/naics6_titles.tsv'):
    m=re.match(r'BAD "(\d{6})","NAICS(\d*) (\d{6}) (.*)"$',l.strip()); assert m and m[1]==m[3],l; ind[m[1]]=m[4]; vint[m[1]]=m[2]
area={"24001":"Allegany County","24003":"Anne Arundel County","24005":"Baltimore County","24009":"Calvert County","24011":"Caroline County","24013":"Carroll County","24015":"Cecil County","24017":"Charles County","24019":"Dorchester County","24021":"Frederick County","24023":"Garrett County","24025":"Harford County","24027":"Howard County","24029":"Kent County","24031":"Montgomery County","24033":"Prince George's County","24035":"Queen Anne's County","24037":"St. Mary's County","24039":"Somerset County","24041":"Talbot County","24043":"Washington County","24045":"Wicomico County","24047":"Worcester County","24510":"Baltimore City","24999":"Unknown Or Undefined, Maryland"}
own={"1":"Federal Government","2":"State Government","3":"Local Government","5":"Private"}
rows=[];hdr=None
for f in sorted(glob.glob(R+'/24[0-9][0-9][0-9].csv')):
    rd=csv.DictReader(open(f)); hdr=hdr or rd.fieldnames; assert rd.fieldnames==hdr
    rows+= [r for r in rd if r['agglvl_code']=='78']
i0=hdr.index('month1_emplvl'); i1=hdr.index('avg_wkly_wage')
blocks={'disclosure_code':hdr[i0:i1+1],
        'lq_disclosure_code':[c for c in hdr if c.startswith('lq_') and c not in('lq_disclosure_code','lq_qtrly_estabs')],
        'oty_disclosure_code':[c for c in hdr if c.startswith('oty_') and c not in('oty_disclosure_code','oty_qtrly_estabs_chg','oty_qtrly_estabs_pct_chg')]}
nz=collections.Counter()
for r in rows:
    for flag,cols in blocks.items():
        if r[flag]=='N':
            for c in cols:
                if r[c] not in ('0','0.0','0.00',''): nz[c]+=1
                r[c]=''
    r['area_title']=area[r['area_fips']]; r['own_title']=own[r['own_code']]; r['industry_title']=ind.get(r['industry_code'],'')
print('rows',len(rows),'missing titles',{r['industry_code'] for r in rows if not r['industry_title']},
      'old-vintage codes',{r['industry_code']:vint[r['industry_code']] for r in rows if vint.get(r['industry_code'])},'nonzero blanked',dict(nz))
out=['area_fips','area_title','own_code','own_title','industry_code','industry_title']+[c for c in hdr if c not in ('area_fips','own_code','industry_code')]
rows.sort(key=lambda r:(r['area_fips'],r['own_code'],r['industry_code']))
with open(O+'/qcew_md_2022q2_naics6_county_ownership.csv','w',newline='') as fh:
    w=csv.DictWriter(fh,out,extrasaction='ignore'); w.writeheader(); w.writerows(rows)
print(len(out),'columns')
print(collections.Counter(r['disclosure_code'] for r in rows))
print('distinct naics6',len({r['industry_code'] for r in rows}))
d=[r for r in rows if r['disclosure_code']=='']; print('disclosed june emp',sum(int(r['month3_emplvl']) for r in d))
print('N rows estabs',sum(int(r['qtrly_estabs']) for r in rows if r['disclosure_code']=='N'),'all estabs',sum(int(r['qtrly_estabs']) for r in rows))
