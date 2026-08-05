(function (global) {
  'use strict';
  var BASE = window.KP_BACKEND || '/bridge';
  function request(path, options) {
    options = options || {}; options.credentials = 'include'; options.headers = options.headers || {}; if (!options.cache) options.cache='no-store';
    if (options.body && typeof options.body !== 'string') { options.headers['Content-Type']='application/json'; options.body=JSON.stringify(options.body); }
    return fetch(BASE + path, options).then(function (r) { if (r.status===202) return r.json(); if (!r.ok) return r.text().then(function(t){throw new Error(t||r.statusText);}); return r.json(); });
  }
  global.KPApi = {
    status:function(){return request('/auth/status');}, profile:function(refresh){return request('/profile'+(refresh?'?refresh=true':''));}, health:function(){return request('/health');},
    startAuth:function(){return request('/auth/device/start',{method:'POST'});}, pollAuth:function(code){return request('/auth/device/poll',{method:'POST',body:{code:code}});}, logout:function(){return request('/auth/logout',{method:'POST'});},
    catalog:function(section,feed,page,nonce){return request('/catalog/list?section='+encodeURIComponent(section||'movie')+'&feed='+encodeURIComponent(feed||'fresh')+'&page='+encodeURIComponent(page||0)+'&perpage=48&_='+encodeURIComponent(nonce||Date.now()));}, pageCount:function(section,feed,refresh){return request('/catalog/page-count?section='+encodeURIComponent(section||'movie')+'&feed='+encodeURIComponent(feed||'fresh')+'&perpage=48'+(refresh?'&refresh=true':''));}, item:function(id){return request('/catalog/items/'+encodeURIComponent(id));}, play:function(id,mediaId){return request('/catalog/items/'+encodeURIComponent(id)+'/play'+(mediaId?'?media_id='+encodeURIComponent(mediaId):''));}, search:function(q,mode){return request('/catalog/search?q='+encodeURIComponent(q||'')+'&mode='+encodeURIComponent(mode||'all')).catch(function(){return request('/mock/search?q='+encodeURIComponent(q||''));});}, autocomplete:function(q){return request('/catalog/autocomplete?q='+encodeURIComponent(q||''));},
    settings:function(){return request('/settings');}, saveSettings:function(v){return request('/settings',{method:'PUT',body:v});}, history:function(){return request('/history');}, watchingStatuses:function(){return request('/watching/statuses');}, saveProgress:function(v){return request('/history',{method:'PUT',body:v});},
    imageProxyUrl:function(url,nonce,width,height,quality){var q=BASE+'/image?url='+encodeURIComponent(url);if(width)q+='&width='+encodeURIComponent(width);if(height)q+='&height='+encodeURIComponent(height);if(quality)q+='&quality='+encodeURIComponent(quality);if(nonce)q+='&v='+encodeURIComponent(nonce);return q;}, streamProxyUrl:function(url){return BASE+'/stream?url='+encodeURIComponent(url);}, subtitleProxyUrl:function(url,offset){var q=BASE+'/subtitle?url='+encodeURIComponent(url),n=Number(offset);if(n)q+='&offset='+encodeURIComponent(n);return q;}, hlsProxyUrl:function(url){return BASE+'/hls?url='+encodeURIComponent(url);},
    hlsAudioVariants:function(url){return request('/media/audio-variants?url='+encodeURIComponent(url));},
    createAudioHls:function(url,track,start){return request('/audio-hls/jobs',{method:'POST',body:{url:url,track:track,start:start||0}});}, audioHlsStatus:function(jobId){return request('/audio-hls/jobs/'+encodeURIComponent(jobId));}, stopAudioHls:function(jobId){return request('/audio-hls/jobs/'+encodeURIComponent(jobId),{method:'DELETE'});},
    report:function(message,details,kind){return request('/debug/events',{method:'POST',body:{kind:kind||'frontend',message:message,details:details||{}}});}, debugEvents:function(){return request('/debug/events');},
    explore:function(path,query){return request('/explorer?path='+encodeURIComponent(path||'')+'&query='+encodeURIComponent(query||''));},
    compareFeeds:function(feed){return request('/catalog/compare-feeds?feed='+encodeURIComponent(feed||'both'));},
    explorerDownloadUrl:function(path,query){return BASE+'/explorer?download=true&path='+encodeURIComponent(path||'')+'&query='+encodeURIComponent(query||'');}
  };
}(window));
