let selected = new Set();
let selectionKind = null;
let currentPath = '';
let art = null;
let currentMedia = null;
let uploadLimits = {
  max_upload_file_size: 100 * 1024 * 1024,
  upload_batch_size: 96 * 1024 * 1024,
  max_batch_files: 200,
};

const $ = id => document.getElementById(id);
const csrf = () => getCookie('__Host-admin_csrf');
function getCookie(name){return document.cookie.split('; ').find(x=>x.startsWith(name+'='))?.split('=').slice(1).join('=')||'';}
function headers(json=true){const h={'X-CSRF-Token':csrf()};if(json)h['Content-Type']='application/json';return h;}
async function api(url, opts={}){
  const r=await fetch(url,opts);
  if(r.status===401){alert('特权模式已失效，请重新提权');location.href='/api/v1/media';throw new Error('unauthorized');}
  const data=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(data.detail||'操作失败');
  return data;
}
function showModal(title, body, onOk){
  $('modalTitle').textContent=title;$('modalBody').innerHTML=body;$('modal').classList.remove('hidden');
  $('modalCancel').onclick=()=>{$('modal').classList.add('hidden')};
  $('modalOk').onclick=async()=>{try{await onOk();$('modal').classList.add('hidden')}catch(e){alert(e.message)}};
}
function updateToolbar(){
  const n=selected.size; $('selection').textContent=n?`已选择 ${n} 个${selectionKind==='directory'?'目录':'文件'}`:'未选择';
  $('play').disabled=!(n===1&&selectionKind==='file');$('download').disabled=!n;$('move').disabled=!n;$('delete').disabled=!n;$('hide').disabled=!(n&&selectionKind==='directory');
  if(n&&selectionKind==='directory'){
    const rows=[...document.querySelectorAll('.tree-row.selected')];$('hide').textContent=rows.every(x=>x.dataset.hidden==='true')?'恢复':'隐藏';
  }else $('hide').textContent='隐藏';
}
function toggleSelection(item){
  if(selectionKind&&selectionKind!==item.kind){selected.clear();selectionKind=null;}
  selectionKind=item.kind;
  if(selected.has(item.path))selected.delete(item.path);else selected.add(item.path);
  updateToolbar();render();
}
async function render(){
  const data=await api('/api/v1/media/admin/tree?path='+encodeURIComponent(currentPath));
  $('pathbar').textContent='/'+currentPath;
  const tree=$('tree');tree.innerHTML='';
  if(currentPath){
    const up=document.createElement('div');up.className='tree-row';up.innerHTML='<span class="kind">↩</span><span class="name">返回上级</span>';up.onclick=()=>{currentPath=currentPath.split('/').slice(0,-1).join('/');render()};tree.appendChild(up)}
  for(const item of data.items){
    const row=document.createElement('div');row.className='tree-row'+(selected.has(item.path)?' selected':'')+(item.hidden?' hidden-item':'');row.dataset.hidden=String(item.hidden);
    row.innerHTML=`<span class="kind">${item.kind==='directory'?'📁':'🎵'}</span><span class="name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span><small>${item.kind==='file'?formatSize(item.size):''}</small>`;
    row.onclick=e=>{e.stopPropagation();toggleSelection(item)};
    row.ondblclick=e=>{e.stopPropagation();if(item.kind==='directory'){currentPath=item.path;selected.clear();selectionKind=null;render()}else{playItem(item)}};
    tree.appendChild(row);
  }
  updateToolbar();
}
function escapeHtml(s){return String(s).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
function formatSize(n){if(n==null)return'';const u=['B','KB','MB','GB'];let i=0;while(n>=1024&&i<3){n/=1024;i++}return `${n.toFixed(i?1:0)} ${u[i]}`}
function buildUploadBatches(files, relativePaths=null){
  const batches=[];let batch=[];let batchBytes=0;
  for(let i=0;i<files.length;i++){
    const item={file:files[i],relativePath:relativePaths?relativePaths[i]:null};
    const exceedsBudget=batch.length>0&&batchBytes+item.file.size>uploadLimits.upload_batch_size;
    const exceedsCount=batch.length>=uploadLimits.max_batch_files;
    if(exceedsBudget||exceedsCount){batches.push(batch);batch=[];batchBytes=0;}
    batch.push(item);batchBytes+=item.file.size;
  }
  if(batch.length)batches.push(batch);
  return batches;
}
async function uploadInBatches(endpoint, fileList, relativePaths, targetDir){
  const files=[...fileList];
  const failed=[];const acceptedFiles=[];const acceptedPaths=[];
  files.forEach((file,index)=>{
    if(file.size>uploadLimits.max_upload_file_size){
      failed.push({name:relativePaths?relativePaths[index]:file.name,error:`文件超过 ${formatSize(uploadLimits.max_upload_file_size)} 限制`});
    }else{
      acceptedFiles.push(file);if(relativePaths)acceptedPaths.push(relativePaths[index]);
    }
  });
  const batches=buildUploadBatches(acceptedFiles,relativePaths?acceptedPaths:null);
  const success=[];
  for(let i=0;i<batches.length;i++){
    $('selection').textContent=`正在上传第 ${i+1}/${batches.length} 批…`;
    const fd=new FormData();fd.append('target_dir',targetDir);
    const rel=[];
    for(const item of batches[i]){fd.append('files',item.file);if(relativePaths)rel.push(item.relativePath);}
    if(relativePaths)fd.append('relative_paths',JSON.stringify(rel));
    const result=await api(endpoint,{method:'POST',headers:headers(false),body:fd});
    success.push(...result.success);failed.push(...result.failed);
  }
  return {success,failed};
}
function playItem(item){
  if(item.kind!=='file'||!item.media)return alert('只能播放音频或视频媒体文件');
  const url='/api/v1/media/stream?file_path='+encodeURIComponent(item.path);
  if(!art){art=new Artplayer({container:'#artplayer',url,title:item.name,autoplay:true,fullscreen:true,fullscreenWeb:true,volume:.7});}
  else art.switchUrl(url);
  if(!art.url||art.url!==url)art.url=url; art.title=item.name; art.play();
}
$('play').onclick=()=>{const path=[...selected][0];const row=[...document.querySelectorAll('.tree-row')].find(x=>x.classList.contains('selected'));if(row)playItem({path,name:row.querySelector('.name').textContent,kind:'file',media:true})};
$('uploadFiles').onclick=()=>$('fileInput').click();
$('uploadFolder').onclick=()=>$('folderInput').click();
$('fileInput').onchange=async e=>{
  if(!e.target.files.length)return;
  if(!currentPath){alert('上传失败，媒体文件禁止直接存放在 data/media 根目录，请先进入子目录');e.target.value='';return;}
  try{const d=await uploadInBatches('/api/v1/media/admin/upload/files',e.target.files,null,currentPath);alert(`上传完成：成功 ${d.success.length}，失败 ${d.failed.length}`);await render()}catch(e){alert(e.message)}e.target.value='';
};
$('folderInput').onchange=async e=>{
  if(!e.target.files.length)return;
  const rel=[...e.target.files].map(f=>f.webkitRelativePath||f.name);
  try{const d=await uploadInBatches('/api/v1/media/admin/upload/folder',e.target.files,rel,currentPath);alert(`上传完成：成功 ${d.success.length}，失败 ${d.failed.length}`);await render()}catch(e){alert(e.message)}e.target.value='';
};
$('delete').onclick=()=>{const paths=[...selected];showModal('确认删除',`将删除选中的 ${paths.length} 个${selectionKind==='directory'?'目录及其全部内容':'文件'}。此操作不可恢复。`,async()=>{await api('/api/v1/media/admin/delete',{method:'POST',headers:headers(),body:JSON.stringify({paths})});selected.clear();selectionKind=null;await render()})};
$('move').onclick=()=>{const paths=[...selected];showModal('移动到目录','<input id="moveTarget" placeholder="例如：明哥/新目录">',async()=>{const dest=$('moveTarget').value.trim();await api('/api/v1/media/admin/move',{method:'POST',headers:headers(),body:JSON.stringify({paths,destination:dest})});selected.clear();selectionKind=null;await render()})};
$('hide').onclick=()=>{const paths=[...selected];const hidden=$('hide').textContent==='隐藏';showModal(hidden?'确认隐藏':'确认恢复',`${hidden?'公共视图将隐藏':'公共视图将恢复显示'}选中的 ${paths.length} 个目录。`,async()=>{await api('/api/v1/media/admin/hide',{method:'POST',headers:headers(),body:JSON.stringify({paths,hidden})});selected.clear();selectionKind=null;await render()})};
$('download').onclick=async()=>{const paths=[...selected];const url='/api/v1/media/admin/download?paths='+encodeURIComponent(JSON.stringify(paths));const r=await fetch(url);if(!r.ok){const d=await r.json().catch(()=>({}));return alert(d.detail||'下载失败')}const blob=await r.blob();const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=paths.length===1?paths[0].split('/').pop():'media-download.zip';a.click();URL.revokeObjectURL(a.href)};
$('backPublic').onclick=()=>location.href='/api/v1/media';
$('logout').onclick=async()=>{try{await fetch('/api/v1/media/admin/logout',{method:'POST',headers:headers(false)})}finally{location.href='/api/v1/media'}};
(async()=>{try{const status=await api('/api/v1/media/admin/status');if(status.limits)uploadLimits={...uploadLimits,...status.limits};await render()}catch(e){}})();
