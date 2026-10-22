/* Native S3 configuration and direct multipart transfer. Secrets stay in forms. */
(() => {
    'use strict';
    const root = '/api/v1/media/admin/s3';
    const post = (path,value={}) => api(root+path,{method:'POST',headers:requestHeaders(),body:JSON.stringify(value)});
    const states = {reserved:'待上传',creating:'建立上传',uploading:'上传中',completing:'校验中',uncertain:'待核对',
        aborting:'待回收',aborted:'已取消',published:'已完成'};
    const make = (tag,text='') => {const el=document.createElement(tag);el.textContent=text;return el;};
    const size = bytes => `${(Number(bytes||0)/1073741824).toFixed(2)} GiB`;
    const panel = document.getElementById('nodesPanel');
    const area = make('div');area.className='s3-admin';area.hidden=true;
    const toolbar = make('div');toolbar.className='nodes-toolbar';
    const summary = make('span'), feedback=make('div'), list=make('div'), pending=make('div');
    feedback.setAttribute('role','status');
    const add=make('button','添加 S3');add.type='button';
    toolbar.append(make('strong','S3'),summary,add);
    area.append(toolbar,list,pending,feedback);
    panel?.querySelector('.nodes-content')?.append(area);
    let busy=false;
    const action = async work => {
        if(busy)return;busy=true;feedback.textContent='处理中';
        try {const result=await work();await refresh();await refreshUploadSiteTypes();feedback.textContent=states[result?.state]||'已完成';}
        catch(error){feedback.textContent=error.message;await refresh().catch(()=>{});}
        finally {busy=false;}
    };
    const button = (label,work) => {const el=make('button',label);el.type='button';el.onclick=()=>action(work);return el;};
    function form(member,recovery=false) {
        const dialog=make('dialog');dialog.className='s3-config-dialog';
        const fields=make('form');fields.autocomplete='off';fields.append(make('h3',recovery?'恢复凭据':member?'配置 S3':'添加 S3'));
        const inputs={};
        const values={name:member?.name||'',bucket:member?.location.bucket||'',region:member?.location.region||'us-east-1',
            endpoint:member?.location.endpoint||'',prefix:member?.location.prefix||'',
            quota:String((member?.quota_bytes||1073741824)/1073741824),style:member?.location.style||'auto'};
        for(const [key,label,type,required] of [['name','名称','text',true],['bucket','存储桶','text',true],
            ['region','区域','text',true],['endpoint','服务地址','url',false],['prefix','目录前缀','text',false],
            ['quota','配额 GiB','number',true],['access_key','Access Key','password',!member],['secret_key','Secret Key','password',!member],
            ['session_token','Session Token','password',false],['expires_at','凭据到期','datetime-local',false]]) {
            if(recovery&&!['access_key','secret_key','session_token','expires_at'].includes(key))continue;
            const row=make('label',label),input=make('input');input.name=key;input.type=type;input.required=required;
            if(recovery&&['access_key','secret_key'].includes(key))input.required=true;
            input.value=values[key]||'';input.autocomplete='off';input.maxLength=key==='session_token'?8192:512;
            if(key==='quota'){input.min='0.001';input.max='8388607';input.step='0.001';}
            row.append(input);fields.append(row);inputs[key]=input;
        }
        const styleRow=make('label','寻址'),style=make('select');
        for(const [value,label] of [['auto','自动'],['path','Path'],['virtual','Virtual']]) {
            const option=make('option',label);option.value=value;style.append(option);
        }
        style.value=values.style;styleRow.append(style);if(!recovery)fields.append(styleRow);
        const error=make('p');error.setAttribute('role','status');
        const save=make('button','保存');save.type='submit';
        const cancel=make('button','取消');cancel.type='button';cancel.onclick=()=>dialog.close();
        fields.append(error,save,cancel);dialog.append(fields);document.body.append(dialog);
        dialog.addEventListener('close',()=>{for(const input of Object.values(inputs))input.value='';dialog.remove();},{once:true});
        fields.onsubmit=async event=>{
            event.preventDefault();save.disabled=true;error.textContent='保存中';
            try {
                const value=key=>inputs[key]?.value.trim()||'';
                const payload=recovery?{revision:member.revision}:{name:value('name'),quota_bytes:Math.floor(Number(value('quota'))*1073741824),
                    location:{bucket:value('bucket'),region:value('region'),endpoint:value('endpoint'),prefix:value('prefix'),style:style.value}};
                if(value('access_key')||value('secret_key'))payload.credentials={access_key:value('access_key'),secret_key:value('secret_key'),
                    session_token:value('session_token'),expires_at:value('expires_at')?Math.floor(new Date(inputs.expires_at.value).getTime()/1000):null};
                else if(value('session_token')||value('expires_at'))throw new Error('请填写完整凭据');
                if(member&&!recovery){payload.revision=member.revision;payload.enabled=member.enabled;}
                await post(recovery?`/members/${member.member_id}/recovery-credentials`:member?`/members/${member.member_id}/configure`:'/members',payload);
                dialog.close();await refresh();await refreshUploadSiteTypes();feedback.textContent='已保存';
            }catch(failure){error.textContent=failure.message;}
            finally{save.disabled=false;}
        };
        dialog.showModal();
    }
    add.onclick=()=>form(null);
    async function refresh({signal} = {}) {
        if(!panel)return;
        const data=await api(root+'/members',{cache:'no-store',signal});
        area.hidden=false;summary.textContent=`${data.members.length} 个`;list.replaceChildren();pending.replaceChildren();
        for(const member of data.members) {
            const details=make('details'),title=make('summary');
            const state=member.credential_state==='expired'?'凭据过期':member.credential_state==='unavailable'?'凭据异常'
                :member.placement_ready?'可上传':member.probe_state==='ready'?'已连接':'未启用';
            title.textContent=`${member.name} · ${state} · ${size(member.used_bytes)} / ${size(member.quota_bytes)}`;
            details.append(title,make('p',`${member.location.bucket} · 预留 ${size(member.reserved_bytes)}`));
            const actions=make('div');actions.className='nodes-toolbar';
            actions.append(button('配置',()=>{form(member);}),button('测试连接',()=>post(`/members/${member.member_id}/probe`,{revision:member.revision})),
                button(member.enabled?'停用':'启用',()=>post(`/members/${member.member_id}/configure`,
                    {revision:member.revision,name:member.name,quota_bytes:member.quota_bytes,enabled:!member.enabled})),
                button('移除',async()=>{if(confirm(`移除 ${member.name}？`))await api(root+`/members/${member.member_id}`,
                    {method:'DELETE',headers:requestHeaders(),body:JSON.stringify({revision:member.revision})});}));
            if(!member.enabled&&member.reserved_bytes>0)actions.append(button('恢复凭据',()=>{form(member,true);}));
            details.append(actions);if(member.error_code)details.append(make('p',member.error_code));list.append(details);
        }
        const dataUploads=await api(root+'/uploads',{cache:'no-store',signal});
        if(dataUploads.truncated)pending.append(make('p','仅显示前 50 项'));
        for(const upload of dataUploads.uploads) {
            const row=make('div');row.className='s3-pending';
            row.append(make('span',`${upload.path.split('/').pop()} · ${states[upload.state]||'待核对'} · ${size(upload.confirmed_bytes)} / ${size(upload.expected_bytes)}`),
                button('核对',()=>post(`/uploads/${upload.upload_id}/reconcile`)),
                button('取消',()=>api(root+`/uploads/${upload.upload_id}`,{method:'DELETE',headers:requestHeaders(false)})));
            pending.append(row);
        }
    }
    document.getElementById('nodesRefresh')?.addEventListener('click',()=>refresh().catch(error=>{feedback.textContent=error.message;}));
    refresh().catch(()=>{});

    function directPart(grant,blob,onProgress) {
        return new Promise((resolve,reject)=>{
            const xhr=new XMLHttpRequest();let idle;
            const touch=()=>{clearTimeout(idle);idle=setTimeout(()=>xhr.abort(),90000);};
            const finish=(error)=>{clearTimeout(idle);error?reject(error):resolve();};
            xhr.open('PUT',grant.url);xhr.withCredentials=false;
            // Content-Length is a forbidden browser header. A Blob body makes
            // the browser supply the exact signed length automatically.
            for(const [name,value] of Object.entries(grant.headers))if(name.toLowerCase()!=='content-length')xhr.setRequestHeader(name,value);
            xhr.upload.onprogress=event=>{touch();if(event.lengthComputable)onProgress(event.loaded);};
            xhr.onerror=()=>finish(new Error('S3 连接失败，上传未确认'));
            xhr.onabort=()=>finish(new Error('S3 上传超时，等待回收'));
            xhr.onload=()=>finish(xhr.status>=200&&xhr.status<300?null:new Error(`S3 上传失败（${xhr.status}）`));
            touch();xhr.send(blob);
        });
    }
    async function transfer(reservation,file,onProgress,onState=()=>{}) {
        const prefix=`/uploads/${reservation.upload_id}`;
        let timer,touching=false;
        try {
            onState('建立上传');const status=await post(prefix+'/initialize');
            if(status.state!=='uploading')throw new Error('上传尚未就绪');
            timer=setInterval(async()=>{if(touching)return;touching=true;try{await post(prefix+'/touch');}catch(_){}finally{touching=false;}},45000);
            for(let offset=0,number=1;offset<file.size;offset+=reservation.part_bytes,number++) {
                onState('上传中');const chunk=file.slice(offset,Math.min(file.size,offset+reservation.part_bytes));
                const hash=new Uint8Array(await crypto.subtle.digest('SHA-256',await chunk.arrayBuffer()));
                const checksum=btoa(String.fromCharCode(...hash));
                let confirmed=false;
                for(let attempt=0;attempt<2&&!confirmed;attempt++) {
                    const grant=await post(prefix+`/parts/${number}`,{checksum_sha256:checksum});
                    try {
                        await directPart(grant,chunk,loaded=>onProgress(Math.min(.99,(offset+loaded)/file.size)));
                        onState('确认中');await post(prefix+`/parts/${number}/confirm`);confirmed=true;
                    }catch(error){if(attempt===1)throw error;}
                }
                onProgress(Math.min(.99,(offset+chunk.size)/file.size));
            }
            onState('校验中');const result=await post(prefix+'/complete');
            if(result.state!=='published')throw new Error('尚待存储确认');
            onProgress(1);return result;
        }finally{clearInterval(timer);refresh().catch(()=>{});}
    }
    window.FrontierS3={transfer,refresh};
})();
