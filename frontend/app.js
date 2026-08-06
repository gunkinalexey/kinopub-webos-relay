(function(){'use strict';
var state={route:'popular',settings:{stream_mode:'auto',app_icon:'kinopub'},current:null,authPoll:null,authTick:null,streamUrl:'',mode:'direct',episodeId:'',catalogCache:{},catalogRequest:0,cacheVersion:Date.now(),catalogPages:{},catalogTotals:{},searchTimer:null,searchSeq:0,suggestionIndex:-1,searchMode:'all',currentSuggestions:[],profile:null,profileCheckedAt:0,authenticated:false,appInitialized:false,authRequired:false,sessionExpired:false,watchedMap:{},historyType:'',mediaCaps:null,hdrAttempt:false,hdrFellBack:false,detailsTab:'plot',detailsSeason:0,detailsReturn:'catalogScreen',detailsFocus:null,playerResumePosition:0,playerSwitching:false,playerStreams:[],playerSubtitles:[],playerAudios:[],playerQualityIndex:'',playerSubtitleChoice:'off',playerAudioChoice:'auto',audioApplyTimer:null,subtitleApplyTimer:null,subtitleMountKey:'',playerOriginalDuration:0,hls:null,hlsManifestReady:false,audioHlsActive:false,audioHlsOffset:0,audioHlsJobId:'',audioHlsPendingJobId:'',audioHlsPollToken:0,audioHlsPreparing:false,audioHlsSelectedIndex:-1,baseStreamUrl:'',baseStreamMode:'direct',streamSwitchSeq:0,expectedTracks:0,altAudioProbe:{},altAudioUrl:'',pendingAltAudioIndex:-1};
var $=function(id){return document.getElementById(id);},video=$('video');
// Every backend call that needs a session (catalog, history, profile, item
// details...) raises a real HTTP 401 the moment the cookie is gone or
// KinoPub's refresh token died server-side - api.js tags the thrown Error
// with .status for exactly this. Wrapping every KPApi method once here means
// any such call, anywhere in the app, re-shows the same device-code gate the
// user sees on first launch instead of leaving a raw error string on screen.
function wrapAuthCheck(name){var orig=KPApi[name];if(typeof orig!=='function')return;KPApi[name]=function(){var result=orig.apply(KPApi,arguments);if(result&&typeof result.catch==='function')result=result.catch(function(err){if(err&&err.status===401)handleSessionExpired();throw err;});return result;};}
(function(){var names=Object.keys(KPApi);for(var i=0;i<names.length;i++)wrapAuthCheck(names[i]);})();

var dogBrandIcons=['assets/dog-icon-1.png','assets/dog-icon-2.png','assets/dog-icon-3.png'];
var dogBrandIndex=-1,brandingRotationTimer=null;
function normalizeAppIcon(value){return value==='kinopub'?'kinopub':'kinoterk';}
function selectedAppIcon(){var nodes=document.querySelectorAll('input[name="appIcon"]');for(var i=0;i<nodes.length;i++)if(nodes[i].checked)return normalizeAppIcon(nodes[i].value);return 'kinopub';}
function setFavicon(src,type){var link=$('appFavicon');if(!link){link=document.createElement('link');link.id='appFavicon';link.rel='icon';document.head.appendChild(link);}link.type=type||(/\.svg(?:$|\?)/i.test(src)?'image/svg+xml':'image/png');link.href=src;}
function nextDogBrandIcon(){var next;if(dogBrandIcons.length<2)next=0;else{do{next=Math.floor(Math.random()*dogBrandIcons.length);}while(next===dogBrandIndex);}dogBrandIndex=next;var src=dogBrandIcons[next],logo=$('brandLogo');if(logo)logo.src=src;setFavicon(src,'image/png');}
function stopBrandingRotation(){if(brandingRotationTimer){clearInterval(brandingRotationTimer);brandingRotationTimer=null;}}
function applyBranding(iconKey){var key=normalizeAppIcon(iconKey),logo=$('brandLogo'),text=$('brandText');stopBrandingRotation();if(key==='kinopub'){if(logo){logo.src='assets/kp-logo.svg';logo.classList.remove('dog-brand-logo');}if(text)text.textContent='kinopub';document.title='kinopub';setFavicon('assets/kp-logo.svg','image/svg+xml');document.body.setAttribute('data-app-brand','kinopub');return;}if(logo)logo.classList.add('dog-brand-logo');if(text)text.textContent='киноТёрк';document.title='киноТёрк';document.body.setAttribute('data-app-brand','kinoterk');nextDogBrandIcon();brandingRotationTimer=setInterval(nextDogBrandIcon,5*60*1000);}
function appBrandName(){return normalizeAppIcon(state.settings&&state.settings.app_icon)==='kinoterk'?'киноТёрк':'KinoPub';}

var routes={
 popular:{title:'Новинки',mode:'tabs',section:'movie',feed:'popular'},
 new:{title:'Новинки',mode:'tabs',section:'movie',feed:'fresh'},
 hot:{title:'Новинки',mode:'tabs',section:'movie',feed:'hot'},
 movie:{title:'Фильмы',mode:'category',section:'movie',feed:'all',show3d:true},
 '3d':{title:'3D Фильмы',mode:'category',section:'3d',feed:'all',show3d:true},
 history:{title:'История',mode:'history',section:'history',feed:'all'},
 watching:{title:'Я смотрю',mode:'watching'},
 serial:{title:'Сериалы',mode:'category',section:'serial',feed:'all'},
 anime:{title:'Аниме',mode:'category',section:'anime',feed:'all'},
 concert:{title:'Концерты',mode:'category',section:'concert',feed:'all'},
 documovie:{title:'Документальные фильмы',mode:'category',section:'documovie',feed:'all'},
 docuserial:{title:'Документальные сериалы',mode:'category',section:'docuserial',feed:'all'},
 tvshow:{title:'ТВ Шоу',mode:'category',section:'tvshow',feed:'all'},
 sport:{title:'Спорт',mode:'category',section:'sport',feed:'all'},
 settings:{title:'Настройки',mode:'settings'}
};
function esc(v){var d=document.createElement('div');d.textContent=String(v==null?'':v);return d.innerHTML;}
function fmt(s){s=isFinite(s)?Math.max(0,Math.floor(s)):0;var h=Math.floor(s/3600),m=Math.floor(s/60)%60,x=s%60;return(h?(h<10?'0':'')+h+':':'')+(m<10?'0':'')+m+':'+(x<10?'0':'')+x;}
function visibleFocus(){var n=document.querySelectorAll('.focusable:not([disabled])'),a=[];for(var i=0;i<n.length;i++)if(n[i].offsetParent!==null)a.push(n[i]);return a;}
function focusFirst(){var a=visibleFocus();if(a[0])a[0].focus();}
// The backdrop is a real 16:9 image now, so a full 1920x1080 is worth
// asking for; posters come from the 500x750 source instead of 250x375 and
// are no longer upscaled. `fallback` covers items with no wide backdrop.
function bgCss(v,kind,fallback){if(!v)return '#283248';if(/^https?:/i.test(v)){var clean=String(v).replace(/"/g,'');var isBackdrop=kind==='backdrop';var w=isBackdrop?1920:420,h=isBackdrop?1080:630,q=82;var alt=fallback&&/^https?:/i.test(fallback)?String(fallback).replace(/"/g,''):'';return 'center/cover no-repeat url("'+KPApi.imageProxyUrl(clean,state.cacheVersion,w,h,q,alt)+'")';}return v;}
function ratingText(value){var n=parseFloat(value);if(!isFinite(n)||n<0||n>10)return '—';return n.toFixed(1).replace('.0','');}
function kinopubText(item){return ratingText(item.rating);}
function ratings(item){return [kinopubText(item),ratingText(item.imdb_rating),ratingText(item.kinopoisk_rating)];}
function watchedStatus(item){var id=String(item&&item.id||'');if(item&&item.watched===1)return 1;if(item&&item.watched===0)return 0;if(state.watchedMap[id]!==undefined)return state.watchedMap[id];return -1;}
// History entries carry the season/episode this specific view was of
// (0/absent for a movie) plus a real frame grabbed from that episode's file.
// Neither exists on plain catalogue items, so this is a no-op there.
function historyEpisodeTag(item){var season=Number(item.history_season)||0,episode=Number(item.history_episode)||0;if(!season||!episode)return '';return 'S'+pad2(season)+'E'+pad2(episode)+(item.media_title?' · '+item.media_title:'');}
// "Я смотрю" cards carry a real new-episode count from KinoPub's own
// v1/watching/serials - only present there, so this is a no-op elsewhere.
function newEpisodesTag(item){var n=Number(item.watching_new)||0;return n>0?plural(n,'новая серия','новые серии','новых серий'):'';}
// The catalogue *list* payload (unlike a single item's own detail/media
// fetch) never carries subtitle info - only `ac3` (Dolby) and `quality`
// (resolution) are real per-item fields there. A static "Субтитры" icon that
// showed on every card regardless of the actual title was outright wrong;
// better to show nothing for what we can't know than fake data for it.
function posterBadges(item){
 var parts=[];
 if(item.has_dolby)parts.push('<span title="Dolby"><svg><use xlink:href="#i-dolby"></use></svg></span>');
 var q=Number(item.quality)||0;
 if(q>=2160)parts.push('<span title="4K">4K</span>');
 else if(q>=720)parts.push('<span title="HD"><svg><use xlink:href="#i-hd"></use></svg></span>');
 return parts.length?'<div class="poster-badges">'+parts.join('')+'</div>':'';
}
function card(item){var p=ratings(item),status=watchedStatus(item),b=document.createElement('button');b.className='media-card focusable';var newTag=newEpisodesTag(item);var mark=newTag?'<div class="new-episodes-badge">'+esc(newTag)+'</div>':(status===1?'<div class="watched-overlay"><span>ПРОСМОТРЕНО</span></div>':(status===0?'<div class="continue-overlay"><span>ПРОДОЛЖИТЬ</span></div>':''));var episodeTag=historyEpisodeTag(item),subtitle=episodeTag||item.original_title||'';var useFrame=episodeTag&&item.episode_thumbnail&&(!state.settings||state.settings.history_episode_frames!==false);b.innerHTML='<div class="poster-art">'+mark+posterBadges(item)+'<div class="poster-ratings"><span><svg><use xlink:href="#i-thumb"></use></svg>'+p[0]+'</span><span><svg><use xlink:href="#i-imdb"></use></svg>'+p[1]+'</span><span><svg><use xlink:href="#i-kp"></use></svg>'+p[2]+'</span></div></div><div class="item-title">'+esc(item.title||'')+'</div><div class="item-author">'+esc(subtitle)+'</div>';var poster=b.querySelector('.poster-art');if(poster)poster.style.background=bgCss(useFrame?item.episode_thumbnail:item.poster,'poster',useFrame?item.poster:'');b.onclick=function(){state.current=item;openDetails(item);};b.onfocus=function(){state.current=item;};return b;}
function renderTop(){var cfg=routes[state.route]||routes.popular,root=$('catalogTop');root.innerHTML='';if(cfg.mode==='tabs'){var row=document.createElement('div');row.className='tab-row';var tabs=[['popular','Популярные'],['new','Свежие'],['hot','Горячие']];for(var i=0;i<tabs.length;i++){var bt=document.createElement('button');bt.className='focusable catalog-tab'+(state.route===tabs[i][0]?' active':'');bt.textContent=tabs[i][1];bt.onclick=(function(r){return function(){route(r);};}(tabs[i][0]));row.appendChild(bt);}root.appendChild(row);return;}var head=document.createElement('div');head.className='catalog-title-row';var title='<h3>';if(cfg.show3d)title+='<button class="focusable title-link'+(state.route!=='3d'?' title-current':'')+'" data-route-inline="movie">Фильмы</button><span class="title-sep">&nbsp;</span><button class="focusable title-link'+(state.route==='3d'?' title-current':'')+'" data-route-inline="3d">3D</button>';else title+=esc(cfg.title);title+='</h3><button id="filterToggle" class="focusable filter-toggle">Фильтры ▾</button>';head.innerHTML=title;root.appendChild(head);var m=head.querySelector('[data-route-inline="movie"]');if(m)m.onclick=function(){route('movie');};var f=head.querySelector('[data-route-inline="3d"]');if(f)f.onclick=function(){route('3d');};var t=head.querySelector('#filterToggle');if(t)t.onclick=toggleFilters;}
function toggleFilters(){var old=$('filterPanel');if(old){old.parentNode.removeChild(old);return;}var panel=document.createElement('div');panel.id='filterPanel';panel.className='filter-panel';var labels=['Жанр','Страна','Год','Качество','Субтитры','Сортировка'];for(var i=0;i<labels.length;i++)panel.innerHTML+='<label>'+labels[i]+'<select class="focusable"><option>Любые</option></select></label>';$('catalogTop').appendChild(panel);setTimeout(focusFirst,10);}
// The grid is CSS auto-fill (`repeat(auto-fill,minmax(165px,1fr))`), so the
// browser already resolves it to one fixed-width track per column before any
// cards are even in it - reading the computed style back gives the exact
// column count for the current viewport without duplicating that layout math
// here. jsdom (the test harness) never runs layout, so its computed value is
// just the literal `repeat(...)` text with no `px` tokens - that reads as 0
// columns, which callers treat as "unknown, use the old flat default".
function gridColumns(){var g=$('catalogGrid'),dv=document.defaultView;if(!g||!dv||!dv.getComputedStyle)return 0;var tmpl=(dv.getComputedStyle(g).gridTemplateColumns||'').trim();if(!tmpl)return 0;var parts=tmpl.split(/\s+/),cols=0;for(var i=0;i<parts.length;i++)if(/px$/.test(parts[i]))cols++;return cols;}
// KinoPub always fills a page to `perpage` except the last, so whatever
// doesn't divide evenly by the row width leaves a ragged last row on every
// page in between, not just the final one. Picking the row count closest to
// 50/cols keeps the total near 50 while landing on a whole number of rows -
// 7/row -> 7 rows (49), 8/row -> 6 rows (48), 6/row -> 8 rows (48).
function catalogPerPage(){var cols=gridColumns();if(!cols)return state.catalogPerPage||48;var rows=Math.max(1,Math.round(50/cols));var perpage=rows*cols;if(perpage!==state.catalogPerPage){state.catalogPerPage=perpage;state.catalogCache={};state.catalogPages={};state.catalogTotals={};}return perpage;}
function catalogPageKey(cfg){return cfg.mode==='history'?('history:'+(state.historyType||'all')):(cfg.section+':'+cfg.feed);}
function currentCatalogPage(cfg){var key=catalogPageKey(cfg);return state.catalogPages[key]||0;}
function setCatalogPage(page){var cfg=routes[state.route]||routes.popular,key=catalogPageKey(cfg);state.catalogPages[key]=Math.max(0,page||0);renderCatalog();setTimeout(function(){try{$('catalogTop').scrollIntoView(true);}catch(e){}},20);}
function pageButton(label,page,active,disabled,extraClass){var b=document.createElement('button');b.className='focusable page-button'+(active?' active':'')+(extraClass?' '+extraClass:'');b.textContent=label;b.disabled=!!disabled;if(!disabled)b.onclick=function(){setCatalogPage(page);};return b;}
function renderPagination(meta,itemCount){
 var root=$('catalogPagination');root.innerHTML='';
 var current=Math.max(0,parseInt(meta&&meta.page,10)||0),totalPages=Math.max(0,parseInt(meta&&meta.total_pages,10)||0),knownTotal=totalPages>0,hasItems=itemCount>0,pageFull=itemCount>=(meta&&meta.perpage||48);
 if(!hasItems&&current===0){root.classList.add('hidden');return;}
 root.classList.remove('hidden');
 // KinoPub keeps ten numeric buttons around the current page. Near the end,
 // show the final ten pages instead of only the last two or three.
 var start=knownTotal?Math.max(0,Math.min(current-5,Math.max(0,totalPages-10))):Math.max(0,current-5);
 var end=knownTotal?Math.min(totalPages,start+10):start+10;
 var definitelyLast=!pageFull && hasItems;
 if(current>0){
   root.appendChild(pageButton('<<',0,false,false,'first'));
   root.appendChild(pageButton('<',Math.max(0,current-1),false,false,'prev'));
 }
 for(var i=start;i<end;i++)root.appendChild(pageButton(String(i+1),i,i===current,false,''));
 var canNext=!definitelyLast && (!knownTotal || current+1<totalPages || pageFull);
 var lastTarget=knownTotal?Math.max(0,totalPages-1):end;
 var canLast=knownTotal?current<totalPages-1:pageFull;
 if(canNext)root.appendChild(pageButton('>',current+1,false,false,'next'));
 if(canLast)root.appendChild(pageButton('>>',lastTarget,false,false,'last'));
}
function discoverTotalPages(cfg,meta,itemCount,forceRefresh){
 var key=catalogPageKey(cfg),known=state.catalogTotals[key];
 if(known>1&&!forceRefresh){meta.total_pages=Math.max(known,meta.total_pages||0);renderPagination(meta,itemCount);return;}
 KPApi.pageCount(cfg.section,cfg.feed,meta.perpage,!!forceRefresh).then(function(info){
   var total=parseInt(info&&info.total_pages,10)||0;if(!total)return;
   // Treat probed totals as a lower bound. Shortcut feeds can expose more pages
   // than an earlier probe found, so never shrink a total already reached.
   total=Math.max(total,(parseInt(meta.page,10)||0)+1,state.catalogTotals[key]||0);
   state.catalogTotals[key]=total;meta.total_pages=total;
   for(var cacheKey in state.catalogCache){if(cacheKey.indexOf(key+':')===0)state.catalogCache[cacheKey].meta.total_pages=Math.max(state.catalogCache[cacheKey].meta.total_pages||0,total);}
   renderPagination(meta,itemCount);
 }).catch(function(err){KPApi.report('Page count discovery failed',{section:cfg.section,feed:cfg.feed,error:String(err)},'catalog').catch(function(){});});
}
var HISTORY_TABS=[['','Все'],['movie','Фильмы'],['serial','Сериалы'],['3d','3D'],['concert','Концерты'],['documovie','Докуфильмы'],['docuserial','Докусериалы'],['tvshow','ТВ Шоу']];
function setHistoryType(type){state.historyType=type||'';renderCatalog();}
function renderHistoryTabs(root){var row=document.createElement('div');row.className='tab-row history-tabs';for(var i=0;i<HISTORY_TABS.length;i++){var b=document.createElement('button');b.className='focusable catalog-tab'+((state.historyType||'')===HISTORY_TABS[i][0]?' active':'');b.textContent=HISTORY_TABS[i][1];b.onclick=(function(t){return function(){setHistoryType(t);};}(HISTORY_TABS[i][0]));row.appendChild(b);}root.appendChild(row);}
// KinoPub returns history newest first; the site groups it under day headings.
function historyDayKey(ts){var d=new Date((Number(ts)||0)*1000);return isFinite(d.getTime())?(d.getFullYear()+'-'+(d.getMonth()+1)+'-'+d.getDate()):'';}
function historyDayLabel(ts){var n=Number(ts)||0;if(!n)return 'Без даты';var d=new Date(n*1000),today=new Date(),yday=new Date();yday.setDate(today.getDate()-1);if(historyDayKey(ts)===historyDayKey(today.getTime()/1000))return 'Сегодня';if(historyDayKey(ts)===historyDayKey(yday.getTime()/1000))return 'Вчера';try{return d.toLocaleDateString('ru-RU',{day:'numeric',month:'long',year:'numeric'});}catch(e){return d.toDateString();}}
function renderHistoryItems(items){var g=$('catalogGrid');g.className='history-list';g.innerHTML='';if(!items.length){g.innerHTML='<p class="empty-state">В истории просмотров пусто</p>';return;}var currentKey=null,grid=null;for(var i=0;i<items.length;i++){var item=items[i],key=historyDayKey(item.watched_at);if(key!==currentKey){currentKey=key;var head=document.createElement('h4');head.className='history-day';head.textContent=historyDayLabel(item.watched_at);g.appendChild(head);grid=document.createElement('div');grid.className='poster-grid';g.appendChild(grid);}grid.appendChild(card(item));}}
function renderHistory(cfg){
 var root=$('catalogTop');root.innerHTML='';
 var head=document.createElement('div');head.className='catalog-title-row';head.innerHTML='<h3>История просмотров</h3>';
 root.appendChild(head);renderHistoryTabs(root);
 var page=currentCatalogPage(cfg),g=$('catalogGrid');
 g.className='history-list';g.innerHTML='<p class="empty-state">Загружаем историю…</p>';
 $('catalogPagination').classList.add('hidden');
 var requestId=++state.catalogRequest;
 KPApi.kinoHistory(page,state.historyType).then(function(data){
  if(requestId!==state.catalogRequest)return;
  var items=(data&&data.items)||[],meta={page:data&&data.page||0,total_pages:data&&data.total_pages||0,total_items:data&&data.total_items||0,has_next:!!(data&&data.has_next)};
  renderHistoryItems(items);renderPagination(meta,items.length);
 }).catch(function(err){
  if(requestId!==state.catalogRequest)return;
  g.innerHTML='<p class="empty-state">Не удалось загрузить историю: '+esc(err&&err.message?err.message:String(err))+'</p>';
  $('catalogPagination').classList.add('hidden');
 });
}
// "Я смотрю" - series being followed that have new, not-yet-watched
// episodes (KinoPub's real v1/watching/serials, not a history scan - that
// would have meant "everything ever watched" instead of "what's new").
function updateWatchingCount(n){var chip=$('watchingCount');if(!chip)return;chip.textContent=n>0?String(n):'';chip.classList.toggle('hidden',!n);}
function renderWatching(cfg){
 renderTop();
 var g=$('catalogGrid');
 g.className='poster-grid';$('catalogPagination').classList.add('hidden');
 g.innerHTML='<p class="empty-state">Загружаем…</p>';
 var requestId=++state.catalogRequest;
 KPApi.watchingList().then(function(data){
  if(requestId!==state.catalogRequest)return;
  var items=(data&&data.items)||[];
  renderCatalogItems(items);
  updateWatchingCount(items.length);
 }).catch(function(err){
  if(requestId!==state.catalogRequest)return;
  g.innerHTML='<p class="empty-state">Не удалось загрузить раздел: '+esc(err&&err.message?err.message:String(err))+'</p>';
  KPApi.report('Watching list failed',{error:String(err)},'catalog').catch(function(){});
 });
}
function renderCatalog(){
 var cfg=routes[state.route]||routes.popular;
 if(cfg.mode==='history'){renderHistory(cfg);return;}
 if(cfg.mode==='watching'){renderWatching(cfg);return;}
 renderTop();
 var g=$('catalogGrid'),page=currentCatalogPage(cfg);
 g.className='poster-grid';
 var perpage=catalogPerPage(),cacheKey=catalogPageKey(cfg)+':'+page+':'+perpage;
 g.innerHTML='<p class="empty-state">Загрузка раздела…</p>';$('catalogPagination').classList.add('hidden');
 var cached=state.catalogCache[cacheKey];
 if(cached){renderCatalogItems(cached.items);renderPagination(cached.meta,cached.items.length);if(page===0||!cached.meta.total_pages||cached.meta.total_pages<=1)discoverTotalPages(cfg,cached.meta,cached.items.length);return;}
 var requestId=++state.catalogRequest;
 KPApi.catalog(cfg.section,cfg.feed,page,state.cacheVersion,perpage).then(function(data){
   if(requestId!==state.catalogRequest)return;
   var items=(data&&data.items)||[],meta={page:data&&data.page||0,total_pages:data&&data.total_pages||0,total_items:data&&data.total_items||0,has_next:!!(data&&data.has_next),perpage:perpage};
   var totalKey=catalogPageKey(cfg),reached=(parseInt(meta.page,10)||0)+1;
   if(reached>1&&reached>(state.catalogTotals[totalKey]||0))state.catalogTotals[totalKey]=reached;
   meta.total_pages=Math.max(meta.total_pages||0,state.catalogTotals[totalKey]||0);
   state.catalogCache[cacheKey]={items:items,meta:meta};
   renderCatalogItems(items);renderPagination(meta,items.length);
   if(page===0||!meta.total_pages||meta.total_pages<=1)discoverTotalPages(cfg,meta,items.length,false);
   else if(items.length>=perpage&&meta.total_pages&&reached>=meta.total_pages)discoverTotalPages(cfg,meta,items.length,true);
 }).catch(function(err){
   if(requestId!==state.catalogRequest)return;
   g.innerHTML='<p class="empty-state">Не удалось загрузить раздел: '+esc(err&&err.message?err.message:String(err))+'</p>';$('catalogPagination').classList.add('hidden');
   KPApi.report('Catalogue section failed',{route:state.route,section:cfg.section,feed:cfg.feed,page:page,error:String(err)},'catalog').catch(function(){});
 });
}
function renderCatalogItems(items){var g=$('catalogGrid');g.innerHTML='';for(var i=0;i<items.length;i++)g.appendChild(card(items[i]));if(!items.length)g.innerHTML='<p class="empty-state">В этом разделе '+esc(appBrandName())+' пока не вернул контент</p>';}

function resetNavigationState(name){var cfg=routes[name];if(cfg){state.catalogPages[catalogPageKey(cfg)]=0;}var input=$('searchInput');if(input)input.value='';state.currentSuggestions=[];state.suggestionIndex=-1;if(state.searchTimer){clearTimeout(state.searchTimer);state.searchTimer=null;}state.searchSeq++;hideSuggestions();}
// Screens are mutually exclusive panes inside the content shell. The details
// view is one of them now, so opening a card replaces the grid instead of
// covering it, and Back returns to whichever screen you came from.
var SCREENS=['catalogScreen','searchScreen','settingsScreen','detailsScreen'];
function showScreen(id){for(var i=0;i<SCREENS.length;i++)$(SCREENS[i]).classList.toggle('hidden',SCREENS[i]!==id);}
function visibleScreen(){for(var i=0;i<SCREENS.length;i++)if(!$(SCREENS[i]).classList.contains('hidden'))return SCREENS[i];return 'catalogScreen';}
// Browser back/forward + reload-keeps-your-place, both from one mechanism:
// the URL hash. Setting location.hash is itself a real navigation (creates
// a history entry, fires 'hashchange' on back/forward), so there's no need
// for pushState bookkeeping - just encode the current screen into it, and
// let a single hashchange handler re-derive the screen from whatever hash
// is showing (including the one already in the address bar on first load).
var applyingHistoryState=false;
function encodeRouteHash(name){return 'route/'+encodeURIComponent(name);}
function encodeDetailsHash(id){return 'details/'+encodeURIComponent(id);}
function encodeSearchHash(mode,query){return 'search/'+encodeURIComponent(mode||'all')+'/'+encodeURIComponent(query||'');}
// Guarded against a missing `location`/`history` (the test harnesses stub a
// bare `window` with no real navigation), not just a browser quirk.
function pushHash(hash){if(applyingHistoryState||typeof location==='undefined')return;if(location.hash.replace(/^#/,'')===hash)return;location.hash=hash;}
function parseHash(){var raw=typeof location!=='undefined'?String(location.hash||''):'';raw=raw.replace(/^#/,'');var parts=raw.split('/');return {type:parts[0]||'',a:parts[1]!==undefined?decodeURIComponent(parts[1]):'',b:parts[2]!==undefined?decodeURIComponent(parts[2]):''};}
function applyHash(){
 applyingHistoryState=true;
 try{
  if(!$('playerLayer').classList.contains('hidden'))closePlayer();
  var h=parseHash(),leavingDetails=!$('detailsScreen').classList.contains('hidden')&&h.type!=='details';
  if(h.type==='details'&&h.a)openDetails({id:h.a,title:''});
  else if(h.type==='search')doSearch(h.a||'all',h.b);
  else if(h.type==='route'&&h.a)route(h.a);
  else route('popular');
  // Same focus-restore closeDetails() does, since the branches above already
  // handled showing the right screen (route()/doSearch() both call
  // showScreen, which hides detailsScreen same as closeDetails() would).
  if(leavingDetails){var back=state.detailsFocus;state.detailsFocus=null;if(back&&back.focus&&back.offsetParent!==null){try{back.focus();}catch(e){}}}
 } finally {
  applyingHistoryState=false;
 }
}
function route(name){if(name==='settings'){state.route=name;showScreen('settingsScreen');loadSettings();}else{resetNavigationState(name);state.route=name;showScreen('catalogScreen');renderCatalog();}var links=document.querySelectorAll('[data-route]');for(var i=0;i<links.length;i++)links[i].classList.toggle('active',links[i].getAttribute('data-route')===name || (name==='new'&&links[i].getAttribute('data-route')==='popular'));setTimeout(focusFirst,20);pushHash(encodeRouteHash(name));}
function detailsMediaList(item){if(item.seasons&&item.seasons.length){var all=[];for(var s=0;s<item.seasons.length;s++)all=all.concat(item.seasons[s].episodes||[]);return all;}return item.media||[];}
function detailsTrackList(item,field){var media=detailsMediaList(item),seen={},out=[];for(var i=0;i<media.length;i++){var list=media[i][field]||[];for(var j=0;j<list.length;j++){var label=field==='audios'?detailedAudioLabel(list[j],out.length,false):detailedSubtitleLabel(list[j],out.length,false);var body=label.replace(/^\d+\.\s*/,'');if(body&&!seen[body]){seen[body]=true;out.push(body);}}}return out;}
function detailsDurationSeconds(item){var direct=Number(item.duration);if(isFinite(direct)&&direct>0)return direct;var media=detailsMediaList(item);for(var i=0;i<media.length;i++){var d=Number(media[i].duration);if(isFinite(d)&&d>0)return d;}return 0;}
function ratingCell(label,value,votes){var text=ratingText(value);if(text==='—')return '';var out='<span class="accent">'+esc(label)+' '+esc(text)+'</span>';if(votes>0)out+=' <small>/ '+esc(Number(votes).toLocaleString('ru-RU'))+'</small>';return out+' ';}
function detailsInfoRows(item){
 var rows=[],duration=detailsDurationSeconds(item);
 var rating=ratingCell('КП',item.kinopoisk_rating,item.kinopoisk_votes)+ratingCell('IMDb',item.imdb_rating,item.imdb_votes)+ratingCell('KinoPub',item.rating,0);
 if(rating.trim())rows.push(['Рейтинг',rating]);
 if(item.seasons_count>0)rows.push(['Всего',esc(plural(item.seasons_count,'сезон','сезона','сезонов')+' и '+plural(item.episodes_count,'эпизод','эпизода','эпизодов'))]);
 if(item.finished===false)rows.push(['Статус','<span class="accent">Выходит</span>']);
 else if(item.finished===true)rows.push(['Статус','Завершён']);
 if(item.year)rows.push(['Год выхода','<span class="accent">'+esc(item.year)+'</span>']);
 if((item.countries||[]).length)rows.push(['Страна',accentJoin(item.countries)]);
 if((item.genres||[]).length)rows.push(['Жанр',accentJoin(item.genres)]);
 if(item.director)rows.push(['Режиссёр',accentJoin(String(item.director).split(','))]);
 if((item.cast||[]).length)rows.push(['В ролях',accentJoin(item.cast)]);
 if(duration>0)rows.push(['Длительность',esc(fmt(duration))+' <small>/ '+Math.round(duration/60)+' мин</small>']);
 if(item.quality)rows.push(['Качество',esc(item.quality)]);
 var subs=(item.subtitle_langs||[]).map(audioLanguageName);
 if(subs.length)rows.push(['Субтитры',esc(subs.join(', '))]);
 return rows;
}
function plural(n,one,few,many){n=Math.abs(Number(n)||0);var mod10=n%10,mod100=n%100;var word=(mod10===1&&mod100!==11)?one:((mod10>=2&&mod10<=4&&(mod100<10||mod100>=20))?few:many);return n+' '+word;}
function accentJoin(list){var parts=[];for(var i=0;i<list.length;i++){var v=String(list[i]||'').trim();if(v)parts.push('<span class="accent">'+esc(v)+'</span>');}return parts.join(', ');}
function renderDetailsInfo(item){var rows=detailsInfoRows(item),html='';for(var i=0;i<rows.length;i++)html+='<div class="details-info-key">'+esc(rows[i][0])+'</div><div class="details-info-value">'+rows[i][1]+'</div>';$('detailsInfo').innerHTML=html;}
function renderDetailsTabs(item){
 var tabs=[{id:'plot',title:'Сюжет'}],audios=detailsTrackList(item,'audios'),subs=detailsTrackList(item,'subtitles');
 if(audios.length)tabs.push({id:'audio',title:'Аудио'});
 if(subs.length)tabs.push({id:'subs',title:'Субтитры'});
 var root=$('detailsTabs');root.innerHTML='';
 if(state.detailsTab&&!tabs.filter(function(t){return t.id===state.detailsTab;}).length)state.detailsTab='plot';
 function body(id){
  if(id==='audio')return '<ol class="details-track-list"><li>'+audios.map(esc).join('</li><li>')+'</li></ol>';
  if(id==='subs')return '<ol class="details-track-list"><li>'+subs.map(esc).join('</li><li>')+'</li></ol>';
  return esc(item.description||'Описание недоступно.');
 }
 function select(id){state.detailsTab=id;$('detailsTabBody').innerHTML=body(id);var nodes=root.querySelectorAll('.details-tab');for(var i=0;i<nodes.length;i++)nodes[i].classList.toggle('active',nodes[i].getAttribute('data-tab')===id);}
 for(var i=0;i<tabs.length;i++){var b=document.createElement('button');b.className='focusable details-tab';b.setAttribute('data-tab',tabs[i].id);b.textContent=tabs[i].title;b.onclick=(function(id){return function(){select(id);};}(tabs[i].id));root.appendChild(b);}
 select(state.detailsTab||'plot');
}
// A season picker only makes sense when the title genuinely has more than one
// season; a movie (or KinoPub's own "season 1" default for unnumbered media)
// must not grow a one-pill selector.
function hasMultipleSeasons(item){return !!(item.seasons&&item.seasons.length>1);}
function episodeCard(item,entry,index){
 var b=document.createElement('button');b.className='focusable episode-card';
 var number=entry.episode||entry.number||index+1,season=entry.season||1;
 var code=hasMultipleSeasons(item)?('S'+pad2(season)+'E'+pad2(number)):'';
 // Same per-episode resume/watched data the Continue button already reads,
 // now marked on the episode's own card - the grid does this for whole
 // titles, and a multi-episode strip needs the same "seen this one already"
 // signal per card, not just for the title as a whole.
 var mark=episodeWatched(entry)?'<div class="watched-overlay"><span>ПРОСМОТРЕНО</span></div>':(resumableRow(episodeProgress(entry))?'<div class="continue-overlay"><span>ПРОДОЛЖИТЬ</span></div>':'');
 b.innerHTML='<div class="episode-thumb">'+mark+'<div class="episode-badge">Эпизод '+esc(number)+'</div><div class="episode-play">▶</div></div><div class="episode-name">'+esc(entry.title||('Серия '+number))+'</div>'+(code?'<div class="episode-code">'+esc(code)+'</div>':'');
 var thumb=b.querySelector('.episode-thumb');if(thumb&&entry.thumbnail)thumb.style.background=bgCss(entry.thumbnail,'poster');
 b.onclick=function(){play(item,entry);};
 return b;
}
function pad2(v){var n=Number(v);return (isFinite(n)&&n<10&&n>=0?'0':'')+(isFinite(n)?n:v);}
function chevronSvg(dir){return '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="'+(dir==='prev'?'M15 5l-7 7 7 7':'M9 5l7 7-7 7')+'"/></svg>';}
// A long season turns the strip wider than the screen. Rather than a native
// scroll with arrows floating on top of it (which fought remote/mouse clicks
// for the edge card, and could leave a card half-cut at the boundary), this
// is a true paged carousel: the strip only ever sits at an exact multiple of
// one page's width, so a page break never lands mid-card, and the arrows are
// flex siblings of the (clipped) viewport rather than absolutely positioned
// over it, so they never sit on - or steal clicks from - an episode card.
// Dots below jump straight to a page; a card focused by remote/keyboard on a
// hidden page pulls its own page into view, since spatial nav has no idea
// pages exist.
var EPISODE_CARD_SLOT=232+14;
function wireEpisodeCarousel(strip){
 var viewport=document.createElement('div');viewport.className='episode-viewport';
 viewport.appendChild(strip);
 var prev=document.createElement('button');prev.className='focusable episode-nav prev';prev.setAttribute('aria-label','Предыдущие серии');prev.innerHTML=chevronSvg('prev');
 var next=document.createElement('button');next.className='focusable episode-nav next';next.setAttribute('aria-label','Следующие серии');next.innerHTML=chevronSvg('next');
 var row=document.createElement('div');row.className='episode-carousel';
 row.appendChild(prev);row.appendChild(viewport);row.appendChild(next);
 var dots=document.createElement('div');dots.className='episode-dots';
 var outer=document.createElement('div');outer.className='episode-carousel-outer';
 outer.appendChild(row);outer.appendChild(dots);
 var page=0,perPage=1,pageCount=1;
 function measure(){
  // maxWidth caps the viewport to whole cards only (see below) - clear it
  // first so this always measures the row's true available width, not last
  // render's already-capped one.
  viewport.style.maxWidth='none';
  perPage=Math.max(1,Math.floor((viewport.clientWidth+14)/EPISODE_CARD_SLOT));
  pageCount=Math.max(1,Math.ceil(strip.children.length/perPage));
  if(page>pageCount-1)page=pageCount-1;
  // flex:1 would otherwise stretch the viewport past however many whole
  // cards fit, letting the *next* card's edge peek through the clip.
  viewport.style.maxWidth=(perPage*EPISODE_CARD_SLOT-14)+'px';
 }
 function renderDots(){
  dots.innerHTML='';
  if(pageCount<=1)return;
  for(var i=0;i<pageCount;i++){
   var d=document.createElement('button');d.className='focusable episode-dot';d.setAttribute('aria-label','Серии, часть '+(i+1));
   d.onclick=(function(index){return function(){goTo(index);};}(i));
   dots.appendChild(d);
  }
 }
 function apply(){
  strip.style.transform='translateX(-'+(page*perPage*EPISODE_CARD_SLOT)+'px)';
  prev.classList.toggle('hidden',page<=0);
  next.classList.toggle('hidden',page>=pageCount-1);
  var dotEls=dots.querySelectorAll('.episode-dot');
  for(var i=0;i<dotEls.length;i++)dotEls[i].classList.toggle('active',i===page);
 }
 function goTo(index){page=Math.max(0,Math.min(pageCount-1,index));apply();}
 prev.onclick=function(){goTo(page-1);};
 next.onclick=function(){goTo(page+1);};
 strip.addEventListener('focusin',function(e){
  var card=e.target&&e.target.closest?e.target.closest('.episode-card'):null;
  if(!card)return;
  var index=Array.prototype.indexOf.call(strip.children,card);
  if(index>=0)goTo(Math.floor(index/perPage));
 });
 outer.refresh=function(resetToStart){if(resetToStart)page=0;measure();renderDots();apply();};
 outer.showIndex=function(index){goTo(Math.floor(Math.max(0,index)/perPage));};
 setTimeout(function(){outer.refresh();},0);
 return outer;
}
// Walks episodes in playback order and returns the index of the first one
// without a completed progress row, or -1 if every single one is watched.
function firstUnwatchedIndex(list){for(var i=0;i<list.length;i++){if(!episodeWatched(list[i]))return i;}return -1;}
// Same idea across season boundaries, for the season-picker layout.
function firstUnwatchedInSeasons(seasons){
 for(var s=0;s<seasons.length;s++){
  var index=firstUnwatchedIndex(seasons[s].episodes||[]);
  if(index>=0)return {season:s,episode:index};
 }
 return null;
}
// Three shapes, matched to what KinoPub actually sent:
//  - 2+ real seasons  -> season pills, each with its own episode strip;
//  - 1 season (or none, e.g. a flat multi-file miniseries) with 2+ episodes
//    -> a single flat strip, no season pills;
//  - a single file (most movies) -> nothing, the Watch button is enough.
function renderDetailsEpisodes(item){
 var root=$('detailsEpisodes');root.innerHTML='';
 var seasons=item.seasons||[];
 if(seasons.length>1){
  var bar=document.createElement('div');bar.className='details-seasons';
  var label=document.createElement('span');label.className='details-seasons-label';label.textContent='Сезоны:';bar.appendChild(label);
  var strip=document.createElement('div');strip.className='episode-strip';
  var carousel=wireEpisodeCarousel(strip);
  function showSeason(index){
   state.detailsSeason=index;
   var pills=bar.querySelectorAll('.season-pill');
   for(var i=0;i<pills.length;i++)pills[i].classList.toggle('active',Number(pills[i].getAttribute('data-season'))===index);
   strip.innerHTML='';
   var episodes=(seasons[index]&&seasons[index].episodes)||[];
   for(var e=0;e<episodes.length;e++)strip.appendChild(episodeCard(item,episodes[e],e));
   carousel.refresh(true);
  }
  for(var s=0;s<seasons.length;s++){
   var pill=document.createElement('button');pill.className='focusable season-pill';pill.setAttribute('data-season',String(s));
   pill.textContent=String(seasons[s].number);
   pill.onclick=(function(index){return function(){showSeason(index);};}(s));
   bar.appendChild(pill);
  }
  root.appendChild(bar);root.appendChild(carousel);
  var start=Number(state.detailsSeason);
  if(!isFinite(start)||!seasons[start])start=0;
  var startEpisode=0;
  // Neither watched signal has loaded yet on the very first render
  // (openDetails nulls both out and fetches them async) - jumping to "the
  // first unwatched episode" before we know anything would just be season 0
  // anyway, so skip it and let whichever load resolves first re-render with
  // the real answer.
  if(state.detailsProgress||state.detailsWatching){
   var target=firstUnwatchedInSeasons(seasons);
   if(target){start=target.season;startEpisode=target.episode;}
   else{start=seasons.length-1;startEpisode=Math.max(0,((seasons[start].episodes)||[]).length-1);}
   state.detailsSeason=start;
  }
  showSeason(start);
  carousel.showIndex(startEpisode);
  return;
 }
 var episodes=seasons.length===1?(seasons[0].episodes||[]):(item.media||[]);
 if(episodes.length<=1)return;
 var flatStrip=document.createElement('div');flatStrip.className='episode-strip';
 for(var m=0;m<episodes.length;m++)flatStrip.appendChild(episodeCard(item,episodes[m],m));
 var flatCarousel=wireEpisodeCarousel(flatStrip);
 root.appendChild(flatCarousel);
 flatCarousel.refresh();
 if(state.detailsProgress||state.detailsWatching){
  var flatUnwatched=firstUnwatchedIndex(episodes);
  flatCarousel.showIndex(flatUnwatched===-1?episodes.length-1:flatUnwatched);
 }
}
// Saved resume points for the open title. Keyed by episode id, so a series
// continues the episode that was actually left unfinished.
function progressRows(){return (state.detailsProgress&&state.detailsProgress.items)||[];}
function episodeProgress(entry){var id=String(entry&&(entry.id||entry.media_id)||''),rows=progressRows();for(var i=0;i<rows.length;i++)if(String(rows[i].episode_id||'')===id)return rows[i];return null;}
// Two "watched" signals can exist for the same episode: our own local
// resume-progress row (this client only, from /history) and KinoPub's own
// per-video status from `v1/watching` (real, tracked across every device the
// account has used). Either one marking it watched is enough - finishing an
// episode on the TV app or the real site should still cross it off here.
function episodeWatched(entry){
 var row=episodeProgress(entry);
 if(row&&row.completed)return true;
 var kp=state.detailsWatching&&state.detailsWatching.episodes,id=String(entry&&entry.id||'');
 return !!(kp&&id&&kp[id]&&kp[id].watched);
}
// A row is worth resuming only in the middle: not at the very start, and not
// once it is finished.
function resumableRow(row){if(!row)return null;var pos=Number(row.position)||0,dur=Number(row.duration)||0;if(row.completed)return null;if(pos<30)return null;if(dur>0&&pos>dur-60)return null;return row;}
function latestResumable(){var rows=progressRows();for(var i=0;i<rows.length;i++){var r=resumableRow(rows[i]);if(r)return r;}return null;}
function episodeForRow(item,row){if(!row)return null;var id=String(row.episode_id||'');if(!id)return null;var media=detailsMediaList(item);for(var i=0;i<media.length;i++)if(String(media[i].id)===id)return media[i];return null;}
// A single-file movie's one "episode" still carries a title - backend fills
// it with a synthetic "Серия 1" placeholder when KinoPub gives it no real
// name, since that's a sane label for an actual multi-episode strip. It has
// no business appearing next to the Continue button for a movie, which has
// nothing to disambiguate in the first place.
function episodeLabel(item,entry){if(!entry)return '';if(detailsMediaList(item).length<=1)return '';var season=entry.season,number=entry.episode||entry.number;if(!hasMultipleSeasons(item))return entry.title||'';return 'S'+pad2(season||1)+'E'+pad2(number||1)+(entry.title?' · '+entry.title:'');}
// Re-renders read state.current, not the `item` argument: by the time either
// of these resolves, KPApi.item(id) may already have replaced it with the
// enriched item (real .seasons/.media) via renderDetails. Using the stale
// summary-card `item` here would wipe a correctly-rendered episode strip
// back to empty the moment this (often slower) call finally comes back.
function loadItemProgress(item){state.detailsProgress=null;return KPApi.history(item.id).then(function(d){state.detailsProgress=d;if(state.current&&String(state.current.id)===String(item.id)){renderDetailsActions(state.current);renderDetailsEpisodes(state.current);}return d;}).catch(function(){return null;});}
// KinoPub's own watched status (`v1/watching`), separate from the local
// resume table above - covers episodes finished on another device/the real
// site, which this client's own SQLite has no way of knowing about.
function loadItemWatching(item){state.detailsWatching=null;return KPApi.watching(item.id).then(function(d){state.detailsWatching=d;if(state.current&&String(state.current.id)===String(item.id))renderDetailsEpisodes(state.current);return d;}).catch(function(){return null;});}
// Watch buttons. 'Continue' appears only when there is a mid-way position to
// return to; otherwise a single plain 'Watch'.
function renderDetailsActions(item){
 var root=$('detailsActions');if(!root)return;root.innerHTML='';
 var row=latestResumable(),entry=episodeForRow(item,row);
 function button(label,cls,startAt,episode){var b=document.createElement('button');b.className='focusable '+cls;b.textContent=label;b.onclick=function(){play(item,episode||null,startAt);};root.appendChild(b);return b;}
 if(row){
  button('▶ Продолжить '+fmt(Number(row.position)||0),'primary details-play',Number(row.position)||0,entry);
  button('Начать заново','secondary details-play-secondary',0,entry);
  var note=document.createElement('span');note.className='details-resume-note';
  note.textContent=entry?episodeLabel(item,entry):'';
  if(note.textContent)root.appendChild(note);
 }else{
  button('▶ Смотреть','primary details-play',0,null);
 }
 var previous=$('detailsPlay');if(previous)previous.removeAttribute('id');
 var first=root.querySelector('button');if(first)first.id='detailsPlay';
}
// KinoPub's real vote endpoint (`v1/items/vote?id=&like=`) returns the
// updated totals straight back, so the buttons just submit and repaint from
// that response - no separate "did I already vote" field exists, so the
// only thing worth guarding against locally is a double-click spamming two
// votes for one click.
function renderVotes(item){
 var box=$('detailsVotes'),votes=item.votes||{},up=Number(votes.positive)||0,down=Number(votes.negative)||0;
 if(!up&&!down){box.innerHTML='';return;}
 box.innerHTML='<button class="focusable details-vote details-vote-up" title="Нравится">▲ <span class="vote-count">'+esc(up)+'</span></button><button class="focusable details-vote details-vote-down" title="Не нравится">▼ <span class="vote-count">'+esc(down)+'</span></button>';
 var upBtn=box.querySelector('.details-vote-up'),downBtn=box.querySelector('.details-vote-down');
 function castVote(like,chosenBtn){
  if(upBtn.disabled||downBtn.disabled)return;
  upBtn.disabled=true;downBtn.disabled=true;
  KPApi.vote(item.id,like).then(function(result){
   upBtn.querySelector('.vote-count').textContent=result.positive;
   downBtn.querySelector('.vote-count').textContent=result.negative;
   if(result.voted)chosenBtn.classList.add('chosen');
  }).catch(function(){upBtn.disabled=false;downBtn.disabled=false;});
 }
 upBtn.onclick=function(){castVote(1,upBtn);};
 downBtn.onclick=function(){castVote(0,downBtn);};
}
function renderDetails(item){
 state.current=item;
 $('detailsTitle').textContent=item.title||'';
 $('detailsOriginal').textContent=[item.original_title||'',item.year||''].filter(Boolean).join(' · ');
 $('detailsBackdrop').style.background=bgCss(item.backdrop||item.poster,'backdrop',item.backdrop_fallback||item.poster);
 $('detailsPoster').style.background=bgCss(item.poster,'poster');
 renderVotes(item);
 renderDetailsActions(item);
 renderDetailsEpisodes(item);
 renderDetailsTabs(item);
 renderDetailsInfo(item);
 showScreen('detailsScreen');
 setTimeout(function(){var b=$('detailsPlay');if(b)b.focus();else focusFirst();},20);
}
function openDetails(item){
 state.current=item;state.detailsTab='plot';state.detailsSeason=0;
 var from=visibleScreen();if(from!=='detailsScreen'){state.detailsReturn=from;state.detailsFocus=document.activeElement;}
 showScreen('detailsScreen');
 $('detailsTitle').textContent=item.title||'Загрузка…';
 $('detailsOriginal').textContent='';
 $('detailsTabs').innerHTML='';$('detailsInfo').innerHTML='';$('detailsEpisodes').innerHTML='';
 $('detailsActions').innerHTML='';state.detailsProgress=null;loadItemProgress(item);state.detailsWatching=null;loadItemWatching(item);
 $('detailsTabBody').textContent='Получаем сведения и список видео…';
 $('detailsBackdrop').style.background=bgCss(item.backdrop||item.poster,'backdrop',item.backdrop_fallback||item.poster);
 $('detailsPoster').style.background=bgCss(item.poster,'poster');
 KPApi.item(item.id).then(function(full){for(var k in item)if(full[k]===undefined)full[k]=item[k];renderDetails(full);}).catch(function(){renderDetails(item);});
 pushHash(encodeDetailsHash(item.id));
}
function closeDetails(){showScreen(state.detailsReturn||'catalogScreen');var back=state.detailsFocus;state.detailsFocus=null;if(back&&back.focus&&back.offsetParent!==null){try{back.focus();return;}catch(e){}}setTimeout(focusFirst,20);}
// An empty array is truthy, so `result.audios||result.media.audios` silently
// kept the empty list and dropped every track the media node did carry.
function firstNonEmptyList(){for(var i=0;i<arguments.length;i++){var v=arguments[i];if(v&&typeof v!=='string'&&typeof v.length==='number'&&v.length)return Array.prototype.slice.call(v);}return [];}
function optionUrl(value){if(!value)return '';if(typeof value==='string')return value.indexOf('http')===0?value:'';return value.url||value.src||value.file||value.link||'';}
function streamQualityLabel(st,index){var q=st.quality||((st.height||'')+(st.height?'p':''));if(!q)q='Вариант '+(index+1);var extra=[];if(st.codec)extra.push(String(st.codec).toUpperCase());if(st.source_type)extra.push(String(st.source_type).toUpperCase());return String(q)+(extra.length?' · '+extra.join(' · '):'');}
function streamGroupKey(st){if(st&&st.file)return 'file:'+String(st.file);return [st&&st.height||'',st&&st.width||'',st&&st.quality||'',st&&st.codec||''].join('|');}
function preparePlayerOptions(result,selected){var streams=result.streams||[],groups=[],byKey={};for(var i=0;i<streams.length;i++){var st=streams[i];if(!st||!st.url)continue;var key=streamGroupKey(st),group=byKey[key];if(!group){group={quality:st.quality,height:st.height,width:st.width,codec:st.codec,file:st.file,variants:{},url:st.url,source_type:st.source_type,protocol:st.protocol};byKey[key]=group;groups.push(group);}var type=String(st.source_type||st.protocol||'http').toLowerCase();group.variants[type]=st.url;if(type.indexOf('hls')===0&&!group.variants.hls)group.variants.hls=st.url;if((type==='http'||st.protocol==='http')&&!group.variants.http)group.variants.http=st.url;}
groups.sort(function(a,b){return Number(b.height||0)-Number(a.height||0);});for(var g=0;g<groups.length;g++){groups[g].source_type=groups[g].variants.hls?'hls':'http';groups[g].protocol=groups[g].variants.hls?'hls':'http';groups[g].url=groups[g].variants.http||groups[g].variants.hls||groups[g].url;}
state.playerStreams=groups;state.playerSubtitles=firstNonEmptyList(result.subtitles,result.media&&result.media.subtitles);state.playerAudios=firstNonEmptyList(result.audios,result.media&&result.media.audios);state.expectedTracks=Number(result.expected_tracks)||0;state.altAudioProbe={};state.altAudioUrl='';state.pendingAltAudioIndex=-1;if(state.expectedTracks>state.playerAudios.length)KPApi.report('Audio track list is shorter than KinoPub reported',{expected:state.expectedTracks,received:state.playerAudios.length,media_id:result.media&&result.media.id},'media').catch(function(){});var q=$('playerQuality');q.innerHTML='';for(var j=0;j<groups.length;j++){var o=document.createElement('option');o.value=String(j);o.textContent=streamQualityLabel(groups[j],j);q.appendChild(o);}var selectedIndex=bestPlayableGroupIndex();if(selectedIndex<0){selectedIndex=0;for(var k=0;k<groups.length;k++){var variants=groups[k].variants||{};if(selected&&((selected.file&&groups[k].file===selected.file)||variants.http===selected.url||variants.hls===selected.url||variants.hls2===selected.url||variants.hls4===selected.url)){selectedIndex=k;break;}}}state.playerQualityIndex=String(selectedIndex);q.value=state.playerQualityIndex;state.playerSubtitleChoice=preferredSubtitleChoice();state.subtitleMountKey='';applySubtitleSize(state.settings&&state.settings.subtitle_size);populateSubtitleMenu();populateAudioMenu();}
// What this device can actually decode and display. Probed once: the answer
// decides which quality variant is worth asking for, because a 2160p HEVC
// stream is only an upgrade if the hardware can play it.
var HEVC_MIME='video/mp4; codecs="hvc1.1.6.L150.B0"',H264_MIME='video/mp4; codecs="avc1.640028"';
function probeCodec(mime){var el='',mse=false;try{el=video.canPlayType(mime)||'';}catch(e){}try{mse=!!(window.MediaSource&&MediaSource.isTypeSupported&&MediaSource.isTypeSupported(mime));}catch(e){}return {native:el==='probably'||el==='maybe',mse:mse,answer:el||'нет'};}
function mediaCapabilities(){if(state.mediaCaps)return state.mediaCaps;var hevc=probeCodec(HEVC_MIME),h264=probeCodec(H264_MIME),hdr=false,gamut=false;try{hdr=!!(window.matchMedia&&(matchMedia('(dynamic-range: high)').matches||matchMedia('(video-dynamic-range: high)').matches));}catch(e){}try{gamut=!!(window.matchMedia&&matchMedia('(color-gamut: p3)').matches);}catch(e){}state.mediaCaps={hevc:hevc,h264:h264,hdrDisplay:hdr,wideGamut:gamut};return state.mediaCaps;}
function qualityCap(){var raw=state.settings&&state.settings.quality;var n=parseInt(raw,10);return isFinite(n)&&n>0?n:0;}
// Highest variant this device can play, honouring the quality ceiling from
// settings. Groups are already sorted by height, tallest first.
function bestPlayableGroupIndex(){var caps=mediaCapabilities(),cap=qualityCap(),groups=state.playerStreams||[];for(var i=0;i<groups.length;i++){var g=groups[i],h=Number(g.height)||0;if(cap&&h>cap)continue;if(isHevcGroup(g)&&!(caps.hevc.native||caps.hevc.mse))continue;return i;}return groups.length?groups.length-1:-1;}
function isHevcGroup(group){var c=String(group&&group.codec||'').toLowerCase();return c==='hevc'||c==='h265'||c==='hvc1'||c==='hev1'||c==='x265';}
// HEVC/HDR only reaches the panel intact when the platform decoder gets the
// file. Going through MSE (hls.js) commonly drops HDR to SDR on webOS, so a
// direct progressive URL is preferred for those variants when it exists.
function preferredModeFor(group){var manual=state.settings&&state.settings.stream_mode;if(manual&&manual!=='auto')return manual;var caps=mediaCapabilities(),variants=group&&group.variants||{};if(isHevcGroup(group)&&caps.hevc.native&&variants.http)return 'direct';return variants.hls?'hls':'relay';}
function currentQualityGroup(){return state.playerStreams[Number(state.playerQualityIndex)||0]||state.playerStreams[0]||null;}
function streamUrlForGroup(group,mode){if(!group)return '';var variants=group.variants||{};if(mode==='hls')return variants.hls4||variants.hls2||variants.hls||variants.http||group.url||'';return variants.http||variants.hls||variants.hls2||variants.hls4||group.url||'';}
function currentHttpAudioSource(){var group=currentQualityGroup(),variants=group&&group.variants||{};return variants.http||'';}
// Track labels are assembled from whatever metadata KinoPub happens to send.
// Both audio and subtitle labels append the same way: skip empties, skip
// unrenderable objects, skip case-insensitive duplicates.
function pushLabelPart(parts,value){value=audioScalar(value);if(!value||value==='[object Object]')return;var low=value.toLowerCase();for(var i=0;i<parts.length;i++)if(String(parts[i]).toLowerCase()===low)return;parts.push(value);}
function truthyFlag(value){var low=String(value===undefined||value===null?'':value).toLowerCase();return value===true||value===1||low==='true'||low==='1'||low==='yes';}
function labelWithIndex(parts,index,fallback){return (index+1)+'. '+(parts.length?parts:[fallback+' '+(index+1)]).join(' · ');}
function subtitleFormat(value){var raw=audioScalar(value).toLowerCase();if(!raw)return '';if(raw.indexOf('webvtt')>=0||raw==='vtt')return 'WebVTT';if(raw.indexOf('subrip')>=0||raw==='srt')return 'SRT';if(raw.indexOf('ass')>=0||raw.indexOf('ssa')>=0)return 'ASS/SSA';if(raw.indexOf('pgs')>=0||raw.indexOf('sup')>=0)return 'PGS';return audioScalar(value).toUpperCase();}
function detailedSubtitleLabel(track,index,isNative){var parts=[],language='',title='',translation='',kind='',format='',forced='',hearing='';if(isNative){language=track.language||track.srclang||'';title=track.label||'';kind=track.kind||'';}else if(track&&typeof track==='object'){language=firstAudioValue(track,['language_name','language','lang','locale','iso_639_1','iso_639_2']);title=firstAudioValue(track,['title','name','label','display_name']);translation=firstAudioValue(track,['translation','translator','studio','team','author']);kind=firstAudioValue(track,['kind','type','subtitle_type','category']);format=firstAudioValue(track,['format','codec','extension','format_name']);forced=firstAudioValue(track,['forced','is_forced']);hearing=firstAudioValue(track,['hearing_impaired','sdh','is_sdh']);}else{title=String(track||'');}var langName=audioLanguageName(language);if(langName)parts.push(langName);pushLabelPart(parts,title);pushLabelPart(parts,translation);var kindLow=String(kind||'').toLowerCase();if(kindLow==='captions')pushLabelPart(parts,'для слабослышащих');else if(kindLow&&kindLow!=='subtitles')pushLabelPart(parts,kind);if(truthyFlag(forced))pushLabelPart(parts,'форсированные');if(truthyFlag(hearing))pushLabelPart(parts,'SDH');pushLabelPart(parts,subtitleFormat(format));return labelWithIndex(parts,index,'Субтитры');}
function subtitleHasUrl(track){return !!optionUrl(track);}
function langKey(value){return String(value||'').trim().toLowerCase().replace('_','-').split('-')[0].slice(0,2);}
function subtitleLanguage(track){return audioScalar(track&&typeof track==='object'&&(track.language||track.lang||track.locale))||'';}
// KinoPub ships a per-track sync correction; a locally remuxed HLS additionally
// starts at a non-zero point. Both shift the same timeline, so they add up.
function subtitleShiftSeconds(track){var raw=track&&typeof track==='object'?(track.shift!=null?track.shift:(track.offset!=null?track.offset:track.delay)):null,n=Number(raw);return isFinite(n)?n:0;}
function subtitleRequestUrl(track){var url=optionUrl(track);if(!url)return '';return KPApi.subtitleProxyUrl(url,subtitleShiftSeconds(track)+(state.audioHlsActive?Number(state.audioHlsOffset)||0:0));}
function externalTrackElements(){return video.querySelectorAll('track[data-kp-external]');}
function currentExternalTrackElement(){var els=externalTrackElements();return els.length?els[els.length-1]:null;}
// Tracks we injected must never be counted as tracks the stream provides,
// otherwise selecting a subtitle grows the menu by one every time.
function embeddedTextTracks(){var out=[];if(!video.textTracks)return out;var els=externalTrackElements();for(var i=0;i<video.textTracks.length;i++){var t=video.textTracks[i],mine=false;for(var j=0;j<els.length;j++)if(els[j].track===t){mine=true;break;}if(!mine)out.push(t);}return out;}
function populateSubtitleMenu(){var select=$('playerSubtitles'),embedded=embeddedTextTracks(),apiCount=state.playerSubtitles&&state.playerSubtitles.length||0,count=Math.max(apiCount,embedded.length);select.innerHTML='<option value="off">Выкл.</option>';for(var i=0;i<count;i++){var apiTrack=i<apiCount?state.playerSubtitles[i]:null,embeddedTrack=i<embedded.length?embedded[i]:null,o=document.createElement('option'),usable=!!embeddedTrack||subtitleHasUrl(apiTrack);o.value='track:'+i;o.textContent=(apiTrack?detailedSubtitleLabel(apiTrack,i,false):detailedSubtitleLabel(embeddedTrack||{},i,true))+(usable?'':' · недоступны');o.disabled=!usable;select.appendChild(o);}select.disabled=count===0;select.value=state.playerSubtitleChoice;if(select.selectedIndex<0){state.playerSubtitleChoice='off';select.value='off';}}
function clearExternalTracks(){var tracks=externalTrackElements();for(var i=0;i<tracks.length;i++)tracks[i].remove();}
function disableAllTextTracks(){if(!video.textTracks)return;for(var i=0;i<video.textTracks.length;i++){try{video.textTracks[i].mode='disabled';}catch(e){}}}
function enableEmbeddedSubtitle(index){var embedded=embeddedTextTracks();if(!embedded[index])return false;disableAllTextTracks();try{embedded[index].mode='showing';return embedded[index].mode==='showing';}catch(e){return false;}}
// Identifies what is currently mounted. Rebuilding the <track> element is only
// justified when one of these inputs actually changed.
function subtitleMountKey(choice){if(!choice||choice==='off')return 'off';var index=Number(String(choice).split(':')[1]),track=state.playerSubtitles[index]||null;return choice+'|'+optionUrl(track)+'|'+subtitleShiftSeconds(track)+'|'+(state.audioHlsActive?Number(state.audioHlsOffset)||0:0);}
function retrySubtitleDisplay(attempt){if(state.subtitleApplyTimer){clearTimeout(state.subtitleApplyTimer);state.subtitleApplyTimer=null;}if(state.playerSubtitleChoice==='off')return;var index=Number(String(state.playerSubtitleChoice).split(':')[1]);if(!isFinite(index)||index<0)return;var el=currentExternalTrackElement(),shown=false;if(el){disableAllTextTracks();if(el.track){try{el.track.mode='showing';shown=el.track.mode==='showing';}catch(e){}}}else shown=enableEmbeddedSubtitle(index);if(!shown&&attempt<8)state.subtitleApplyTimer=setTimeout(function(){retrySubtitleDisplay(attempt+1);},250);}
function applySubtitleChoice(choice,force){choice=choice||'off';var key=subtitleMountKey(choice);
 // Appending a <track> fires addtrack, whose handler calls this function again.
 // Without this guard the element was torn down and rebuilt on every event, so
 // the VTT was re-downloaded in an endless loop and never stayed visible.
 if(!force&&key===state.subtitleMountKey&&choice===state.playerSubtitleChoice){retrySubtitleDisplay(0);return;}
 state.subtitleMountKey=key;state.playerSubtitleChoice=choice;
 if(state.subtitleApplyTimer){clearTimeout(state.subtitleApplyTimer);state.subtitleApplyTimer=null;}
 disableAllTextTracks();clearExternalTracks();
 if(choice==='off')return;
 var index=Number(String(choice).split(':')[1]);if(!isFinite(index)||index<0){state.playerSubtitleChoice='off';state.subtitleMountKey='off';return;}
 var apiTrack=state.playerSubtitles[index]||null,src=subtitleRequestUrl(apiTrack);
 if(src){var el=document.createElement('track');el.setAttribute('data-kp-external','1');el.kind='subtitles';el.label=detailedSubtitleLabel(apiTrack,index,false);el.srclang=subtitleLanguage(apiTrack)||'ru';el.src=src;el.default=true;el.onload=function(){retrySubtitleDisplay(0);};video.appendChild(el);}
 retrySubtitleDisplay(0);}
function preferredSubtitleChoice(){var want=langKey(state.settings&&state.settings.subtitles);if(!want||want==='of')return 'off';var list=state.playerSubtitles||[];for(var i=0;i<list.length;i++)if(subtitleHasUrl(list[i])&&langKey(subtitleLanguage(list[i]))===want)return 'track:'+i;return 'off';}
function normalizeSubtitleSize(value){var n=Number(value);if(!isFinite(n))return 100;var allowed=[75,100,125,150],best=100,gap=1e9;for(var i=0;i<allowed.length;i++){var d=Math.abs(allowed[i]-n);if(d<gap){gap=d;best=allowed[i];}}return best;}
function applySubtitleSize(value){var size=normalizeSubtitleSize(value),layer=$('playerLayer');state.settings.subtitle_size=size;if(layer){var classes=['subs-75','subs-100','subs-125','subs-150'];for(var i=0;i<classes.length;i++)layer.classList.remove(classes[i]);layer.classList.add('subs-'+size);}var picker=$('playerSubtitleSize');if(picker)picker.value=String(size);var setting=$('setSubSize');if(setting)setting.value=String(size);return size;}
function audioLanguageName(code){var raw=String(code||'').trim(),key=raw.toLowerCase().replace('_','-').split('-')[0];var names={ru:'Русский',rus:'Русский',uk:'Украинский',ukr:'Украинский',en:'Английский',eng:'Английский',de:'Немецкий',deu:'Немецкий',ger:'Немецкий',fr:'Французский',fra:'Французский',fre:'Французский',es:'Испанский',spa:'Испанский',it:'Итальянский',ita:'Итальянский',ja:'Японский',jpn:'Японский',ko:'Корейский',kor:'Корейский',zh:'Китайский',zho:'Китайский',chi:'Китайский',pl:'Польский',pol:'Польский',tr:'Турецкий',tur:'Турецкий'};return names[key]||raw;}
function audioScalar(value){if(value===undefined||value===null)return '';if(typeof value==='string'||typeof value==='number'||typeof value==='boolean'){var text=String(value).trim();return text==='[object Object]'?'':text;}if(Array.isArray(value)){for(var i=0;i<value.length;i++){var arrayText=audioScalar(value[i]);if(arrayText)return arrayText;}return '';}if(typeof value==='object'){var preferred=['title','name','label','display_name','short_name','studio','team','author','type','voice','translation','language_name','language','lang','codec','format_name','value'];for(var j=0;j<preferred.length;j++){if(Object.prototype.hasOwnProperty.call(value,preferred[j])){var nested=audioScalar(value[preferred[j]]);if(nested)return nested;}}return '';}return '';}
function firstAudioValue(obj,keys){if(!obj||typeof obj!=='object')return '';for(var i=0;i<keys.length;i++){var v=audioScalar(obj[keys[i]]);if(v)return v;}return '';}
function normalizeChannels(value){var v=String(value||'').trim();if(!v)return '';var low=v.toLowerCase();if(low==='2'||low==='2.0'||low==='stereo')return '2.0';if(low==='1'||low==='1.0'||low==='mono')return '1.0';if(low==='6'||low==='5.1')return '5.1';if(low==='8'||low==='7.1')return '7.1';return v;}
function detailedAudioLabel(track,index,isNative){var parts=[],language='',title='',translation='',voice='',channels='',codec='',bitrate='';if(isNative){language=track.language||track.srclang||'';title=track.label||'';codec=track.codec||'';channels=track.channels||'';}else if(track&&typeof track==='object'){language=firstAudioValue(track,['language_name','language','lang','locale','iso_639_1','iso_639_2']);title=firstAudioValue(track,['title','name','label','display_name']);translation=firstAudioValue(track,['translation','translator','studio','team','author','voice_studio']);voice=firstAudioValue(track,['voice','voice_type','translation_type','type','format']);channels=firstAudioValue(track,['channels','channel_layout','audio_channels']);codec=firstAudioValue(track,['codec','audio_codec','format_name']);bitrate=firstAudioValue(track,['bitrate','audio_bitrate']);}else{title=String(track||'');}
var langName=audioLanguageName(language);if(langName)parts.push(langName);pushLabelPart(parts,title);pushLabelPart(parts,voice);pushLabelPart(parts,translation);pushLabelPart(parts,normalizeChannels(channels));pushLabelPart(parts,codec?String(codec).toUpperCase():'');if(bitrate){var b=Number(bitrate);pushLabelPart(parts,isFinite(b)&&b>1000?Math.round(b/1000)+' kbps':bitrate);}return labelWithIndex(parts,index,'Дорожка');}
function isGenericTrackLabel(value){var text=audioScalar(value).toLowerCase();return !text||/^track\s*\d*$/.test(text)||/^audio\s*\d*$/.test(text)||/^дорожка\s*\d*$/.test(text);}
function mergedAudioLabel(nativeTrack,apiTrack,index){if(apiTrack){var apiLabel=detailedAudioLabel(apiTrack,index,false);if(apiLabel&&apiLabel.indexOf('Дорожка '+(index+1))<0)return apiLabel;}if(nativeTrack&&!isGenericTrackLabel(nativeTrack.label))return detailedAudioLabel(nativeTrack,index,true);if(apiTrack)return detailedAudioLabel(apiTrack,index,false);return detailedAudioLabel(nativeTrack||{},index,true);}
// KinoPub's audios[].index is a per-file track number that is 1-based in most
// payloads and 0-based in some. Deriving a stream index from it picked the
// wrong track whenever the file also carried cover art or embedded subtitles,
// and mapped track 2 onto track 1 for 0-based payloads. The backend now sorts
// audios into file order, so the menu position IS the track position.
function audioTrackOrdinal(listIndex){var n=Number(listIndex);return isFinite(n)&&n>=0?n:0;}
// The audio menu is driven by media.audios. KinoPub's `tracks` field only says
// how many tracks exist, so it is used to warn about a truncated list, never to
// hide entries. A track is addressed by its position in that list, which is the
// same order FFmpeg uses for 0:a:N and hls.js uses for audioTracks[N].
function populateAudioMenu(){var select=$('playerAudio');select.innerHTML='<option value="auto">Авто</option>';var nativeCount=video.audioTracks&&video.audioTracks.length?video.audioTracks.length:0,hlsCount=state.hls&&state.hls.audioTracks?state.hls.audioTracks.length:0,apiCount=state.playerAudios&&state.playerAudios.length?state.playerAudios.length:0,count=Math.max(apiCount,nativeCount,hlsCount);for(var i=0;i<count;i++){var nativeTrack=nativeCount>i?video.audioTracks[i]:null,apiTrack=apiCount>i?state.playerAudios[i]:null,o=document.createElement('option');o.value='track:'+i;o.textContent=mergedAudioLabel(nativeTrack,apiTrack,i);select.appendChild(o);}select.disabled=count===0;select.value=state.playerAudioChoice;if(select.selectedIndex<0){state.playerAudioChoice='auto';select.value='auto';}}
function normalizedTrackText(track){return (((track&&track.name)||'')+' '+((track&&track.label)||'')+' '+((track&&track.lang)||'')+' '+((track&&track.language)||'')).toLowerCase();}
// Position first, metadata second. When the stream exposes exactly as many
// tracks as the API listed, position N is track N; otherwise fall back to
// matching language and studio, then to the raw position.
function resolveAudioTargetIn(tracks,listIndex){var count=tracks&&tracks.length||0;if(!count)return -1;if(listIndex<0)return 0;var apiCount=state.playerAudios&&state.playerAudios.length||0;if(apiCount===count&&listIndex<count)return listIndex;var apiTrack=state.playerAudios[listIndex]||null,lang=audioScalar(apiTrack&&typeof apiTrack==='object'&&(apiTrack.lang||apiTrack.language||apiTrack.locale)).toLowerCase(),author=firstAudioValue(apiTrack,['author','studio','team','translation']).toLowerCase();for(var i=0;i<count;i++){var text=normalizedTrackText(tracks[i]);if(lang&&(text.indexOf(lang)>=0||text.indexOf(audioLanguageName(lang).toLowerCase())>=0))return i;if(author&&author.length>2&&text.indexOf(author)>=0)return i;}return listIndex<count?listIndex:-1;}
function nativeAudioTracks(){return video.audioTracks&&video.audioTracks.length?video.audioTracks:null;}
function hlsAudioTracks(){return state.hls&&state.hls.audioTracks&&state.hls.audioTracks.length?state.hls.audioTracks:null;}
function setNativeAudioTrack(listIndex){var tracks=nativeAudioTracks();if(!tracks)return false;var target=resolveAudioTargetIn(tracks,listIndex);if(target<0||!tracks[target])return false;try{for(var i=0;i<tracks.length;i++)tracks[i].enabled=i===target;return !!tracks[target].enabled;}catch(e){return false;}}
function applyHlsAudio(listIndex){var tracks=hlsAudioTracks();if(!tracks||tracks.length<2)return false;var target=resolveAudioTargetIn(tracks,listIndex);if(target<0)return false;try{state.hls.audioTrack=target;return state.hls.audioTrack===target;}catch(e){return false;}}
// Re-select the wanted track after a reload without re-running the escalation
// ladder, which would otherwise restart the probe (and eventually FFmpeg) on
// every canplay event.
function reapplyAudioSelection(){if(state.audioHlsActive||state.audioHlsPreparing)return;var pending=Number(state.pendingAltAudioIndex),wanted=isFinite(pending)&&pending>=0?pending:Number(String(state.playerAudioChoice||'').split(':')[1]);if(!isFinite(wanted)||wanted<0)return;if(applyHlsAudio(wanted)||(nativeAudioTracks()&&nativeAudioTracks().length>1&&setNativeAudioTrack(wanted))){state.pendingAltAudioIndex=-1;$('playerError').textContent='';return;}
 // The variant was reloaded specifically for this track and still cannot
 // provide it: hand over to the server-side remux.
 if(isFinite(pending)&&pending>=0&&(state.hlsManifestReady||video.readyState>=1)){state.pendingAltAudioIndex=-1;prepareAudioHls(wanted,null,'auto');}}
function destroyHls(){if(state.hls){try{state.hls.destroy();}catch(e){}state.hls=null;}state.hlsManifestReady=false;}
function logicalCurrentTime(){return Math.max(0,(state.audioHlsActive?Number(state.audioHlsOffset)||0:0)+(Number(video.currentTime)||0));}
function logicalDuration(){if(state.playerOriginalDuration>0)return state.playerOriginalDuration;var d=Math.max(0,Number(video.duration)||0);return state.audioHlsActive?d+(Number(state.audioHlsOffset)||0):d;}
function currentResume(){var livePosition=logicalCurrentTime();if(livePosition>0)state.playerResumePosition=livePosition;return {position:Math.max(livePosition,Number(state.playerResumePosition)||0),paused:video.paused||video.ended};}
function prox(url,mode){return mode==='relay'?KPApi.streamProxyUrl(url):mode==='hls'?KPApi.hlsProxyUrl(url):url;}
function clearSwitchMessage(resume){var clearMessage=function(){$('playerError').textContent='';video.removeEventListener('playing',clearMessage);video.removeEventListener('canplay',clearMessage);};video.addEventListener(resume.paused?'canplay':'playing',clearMessage);}
function openUrl(url,mode,episodeId,resume,options){options=options||{};var isAudioHls=!!options.audioHls,offset=isAudioHls?Math.max(0,Number(options.offset)||0):0;if(!isAudioHls){state.audioHlsActive=false;state.audioHlsOffset=0;state.audioHlsJobId='';state.audioHlsSelectedIndex=-1;state.baseStreamUrl=url;state.baseStreamMode=mode||'direct';}else{state.audioHlsActive=true;state.audioHlsOffset=offset;state.audioHlsJobId=options.jobId||'';state.audioHlsSelectedIndex=Number(options.listIndex);}
state.streamUrl=url;state.mode=mode||'direct';state.episodeId=episodeId||'';state.streamSwitchSeq=(state.streamSwitchSeq||0)+1;var switchSeq=state.streamSwitchSeq,resumePosition=resume&&isFinite(resume.position)?Math.max(0,Number(resume.position)):Math.max(0,Number(state.playerResumePosition)||0),resumePaused=!!(resume&&resume.paused);state.playerResumePosition=Math.max(Number(state.playerResumePosition)||0,resumePosition);state.playerSwitching=true;$('playerMode').textContent=isAudioHls?'AUDIO HLS':state.mode.toUpperCase();if($('playerStreamMode')&&!isAudioHls)$('playerStreamMode').value=state.mode;video.pause();video.onloadedmetadata=null;video.oncanplay=null;destroyHls();video.removeAttribute('src');video.load();
// load() discards the old text tracks, so the mounted subtitle must be rebuilt
// even though the choice itself has not changed.
state.subtitleMountKey='';var restored=false;function restorePlayback(){if(restored||switchSeq!==state.streamSwitchSeq)return;restored=true;if(!isAudioHls&&isFinite(video.duration)&&video.duration>0)state.playerOriginalDuration=video.duration;var target=isAudioHls?Math.max(0,resumePosition-offset):resumePosition;if(isFinite(video.duration)&&video.duration>0)target=Math.min(target,Math.max(0,video.duration-.25));try{if(target>0)video.currentTime=target;}catch(e){}state.playerResumePosition=resumePosition;state.playerSwitching=false;populateAudioMenu();populateSubtitleMenu();applySubtitleChoice(state.playerSubtitleChoice);if(state.playerAudioChoice!=='auto'&&!isAudioHls&&!state.audioHlsPreparing)setTimeout(reapplyAudioSelection,0);if(resumePaused){keepPlayerControlsVisible();return;}startPlayback(switchSeq);}
var source=options.localSource?url:prox(url,state.mode);if(state.mode==='hls'&&window.Hls&&Hls.isSupported()){state.hlsManifestReady=false;state.hls=new Hls({enableWorker:true,lowLatencyMode:false,backBufferLength:600,maxBufferLength:90});state.hls.attachMedia(video);state.hls.on(Hls.Events.MEDIA_ATTACHED,function(){if(switchSeq!==state.streamSwitchSeq)return;state.hls.loadSource(source);});state.hls.on(Hls.Events.MANIFEST_PARSED,function(){if(switchSeq!==state.streamSwitchSeq)return;state.hlsManifestReady=true;populateAudioMenu();populateSubtitleMenu();restorePlayback();reapplyAudioSelection();});state.hls.on(Hls.Events.AUDIO_TRACKS_UPDATED,function(){populateAudioMenu();reapplyAudioSelection();});state.hls.on(Hls.Events.SUBTITLE_TRACKS_UPDATED,function(){populateSubtitleMenu();});state.hls.on(Hls.Events.ERROR,function(evt,data){if(data&&data.fatal){$('playerError').textContent='Ошибка HLS: '+(data.details||data.type||'неизвестная ошибка');}});video.onloadedmetadata=restorePlayback;video.oncanplay=restorePlayback;return;}state.hlsManifestReady=false;video.src=source;video.onloadedmetadata=restorePlayback;video.oncanplay=restorePlayback;}
function audioHlsCanSeek(target){if(!state.audioHlsActive)return true;var local=Number(target)-Number(state.audioHlsOffset||0);if(local<0)return false;try{if(video.seekable&&video.seekable.length){for(var i=0;i<video.seekable.length;i++)if(local>=video.seekable.start(i)-.25&&local<=video.seekable.end(i)-.1)return true;return false;}}catch(e){}var d=Number(video.duration)||0;return d>0&&local<d-.1;}
function stopAudioHlsJob(jobId){if(!jobId)return;KPApi.stopAudioHls(jobId).catch(function(){});}
function failAudioHls(message,fallbackResume,previousChoice,token){if(token!==state.audioHlsPollToken)return;if(state.audioHlsPendingJobId){stopAudioHlsJob(state.audioHlsPendingJobId);state.audioHlsPendingJobId='';}state.audioHlsPreparing=false;state.playerAudioChoice=previousChoice||'auto';populateAudioMenu();$('playerError').textContent=message||'Не удалось подготовить выбранную звуковую дорожку.';if(fallbackResume&&!fallbackResume.paused){startPlayback();}else keepPlayerControlsVisible();}
function pollAudioHlsJob(jobId,listIndex,targetResume,fallbackResume,previousChoice,token,attempt){if(token!==state.audioHlsPollToken)return;KPApi.audioHlsStatus(jobId).then(function(info){if(token!==state.audioHlsPollToken)return;if(info.status==='failed'){failAudioHls(info.error||'Не удалось подготовить выбранную звуковую дорожку.',fallbackResume,previousChoice,token);return;}var required=Math.max(0,targetResume.position-Number(info.start_offset||0))+.75;if((info.status==='ready'||info.status==='complete')&&Number(info.available_duration||0)>=required){state.audioHlsPreparing=false;var previousJob=state.audioHlsJobId;state.audioHlsPendingJobId='';if(Number(info.original_duration)>0)state.playerOriginalDuration=Number(info.original_duration);openUrl(info.playlist_url,'hls',state.episodeId,targetResume,{audioHls:true,offset:info.start_offset,jobId:jobId,listIndex:listIndex,localSource:true});clearSwitchMessage(targetResume);if(previousJob&&previousJob!==jobId)stopAudioHlsJob(previousJob);return;}if(info.status==='complete'){failAudioHls('Подготовленный поток закончился раньше выбранной позиции.',fallbackResume,previousChoice,token);return;}if(attempt>=90){failAudioHls('Подготовка дорожки заняла больше 45 секунд. Исходный поток оставлен без изменений.',fallbackResume,previousChoice,token);return;}setTimeout(function(){pollAudioHlsJob(jobId,listIndex,targetResume,fallbackResume,previousChoice,token,attempt+1);},500);}).catch(function(err){failAudioHls('Ошибка подготовки дорожки: '+(err&&err.message?err.message:String(err)),fallbackResume,previousChoice,token);});}
function prepareAudioHls(listIndex,resumeOverride,previousChoice){var realIndex=audioTrackOrdinal(listIndex),source=currentHttpAudioSource(),fallbackResume=currentResume(),targetResume=resumeOverride&&isFinite(resumeOverride.position)?{position:Math.max(0,Number(resumeOverride.position)||0),paused:!!resumeOverride.paused}:fallbackResume;if(!source){state.playerAudioChoice=previousChoice||'auto';populateAudioMenu();$('playerError').textContent='Для выбранного качества KinoPub не вернул исходный HTTP-файл с аудиодорожками.';return;}if(state.audioHlsPendingJobId){stopAudioHlsJob(state.audioHlsPendingJobId);state.audioHlsPendingJobId='';}state.audioHlsPollToken++;var token=state.audioHlsPollToken;state.audioHlsPreparing=true;video.pause();$('playerError').textContent='Готовим выбранную звуковую дорожку…';KPApi.createAudioHls(source,realIndex,targetResume.position).then(function(info){if(token!==state.audioHlsPollToken){if(info&&info.job_id)stopAudioHlsJob(info.job_id);return;}if(!info||!info.job_id){failAudioHls('Backend не вернул идентификатор HLS-задачи.',fallbackResume,previousChoice,token);return;}state.audioHlsPendingJobId=info.job_id;pollAudioHlsJob(info.job_id,listIndex,targetResume,fallbackResume,previousChoice,token,0);}).catch(function(err){failAudioHls('Не удалось запустить подготовку дорожки: '+(err&&err.message?err.message:String(err)),fallbackResume,previousChoice,token);});}
// Ask the backend which of the KinoPub HLS variants carries alternate audio
// renditions. Probing is one small text fetch per variant and the answer is
// cached per quality group, so it costs nothing after the first switch.
function altAudioCandidates(){var group=currentQualityGroup(),variants=group&&group.variants||{},order=['hls4','hls2','hls'],out=[];for(var i=0;i<order.length;i++){var url=variants[order[i]];if(url&&out.indexOf(url)<0)out.push(url);}return out;}
function findAltAudioVariant(){var urls=altAudioCandidates();if(!urls.length)return Promise.resolve('');function step(i){if(i>=urls.length)return Promise.resolve('');var url=urls[i],cached=state.altAudioProbe[url];if(cached!==undefined)return cached>=2?Promise.resolve(url):step(i+1);return KPApi.hlsAudioVariants(url).then(function(info){var count=Number(info&&info.count)||0;state.altAudioProbe[url]=count;return count>=2?url:step(i+1);}).catch(function(){state.altAudioProbe[url]=0;return step(i+1);});}return step(0);}
// Three ways to change audio, cheapest first. FFmpeg is the last resort, used
// only when the stream itself genuinely carries a single track.
function applyAudioChoice(choice){var previousChoice=state.playerAudioChoice||'auto';choice=choice||'auto';if(state.audioApplyTimer){clearTimeout(state.audioApplyTimer);state.audioApplyTimer=null;}if(choice==='auto'){state.playerAudioChoice='auto';state.audioHlsPollToken++;state.audioHlsPreparing=false;if(state.audioHlsPendingJobId){stopAudioHlsJob(state.audioHlsPendingJobId);state.audioHlsPendingJobId='';}var activeJob=state.audioHlsJobId;$('playerError').textContent='';if(state.audioHlsActive){var resume=currentResume(),url=state.baseStreamUrl||streamUrlForGroup(currentQualityGroup(),state.baseStreamMode),mode=state.baseStreamMode||'direct';openUrl(url,mode,state.episodeId,resume);clearSwitchMessage(resume);stopAudioHlsJob(activeJob);}else{applyHlsAudio(-1);setNativeAudioTrack(-1);}return;}
 var index=Number(String(choice).split(':')[1]);if(!isFinite(index)||index<0)return;
 if(state.audioHlsActive&&state.audioHlsSelectedIndex===index&&!state.audioHlsPreparing){state.playerAudioChoice=choice;return;}
 state.playerAudioChoice=choice;
 // 1. Alternate audio in the manifest already loaded: instant, position and
 //    seeking untouched.
 if(!state.audioHlsActive&&applyHlsAudio(index)){$('playerError').textContent='';return;}
 // 2. Native MP4 tracks. LG webOS exposes these on direct playback; desktop
 //    Chrome does not, and falls through.
 if(!state.audioHlsActive&&nativeAudioTracks()&&nativeAudioTracks().length>1&&setNativeAudioTrack(index)){$('playerError').textContent='';return;}
 // 3. Reload a KinoPub HLS variant that does carry several renditions.
 $('playerError').textContent='Ищем поток с этой дорожкой…';
 var seq=state.streamSwitchSeq;
 findAltAudioVariant().then(function(url){
  if(state.playerAudioChoice!==choice)return;
  if(url&&url!==state.altAudioUrl){var resume=currentResume();state.altAudioUrl=url;state.pendingAltAudioIndex=index;openUrl(url,'hls',state.episodeId,resume);clearSwitchMessage(resume);return;}
  if(url&&seq===state.streamSwitchSeq&&applyHlsAudio(index)){$('playerError').textContent='';return;}
  // 4. Nothing in the stream to switch to: server-side remux.
  prepareAudioHls(index,null,previousChoice);
 }).catch(function(){prepareAudioHls(index,null,previousChoice);});
}
// Fullscreen adds no pixels here - the player layer already covers the viewport
// and the webOS browser is itself fullscreen. What it can change is which
// surface draws the video: TV browsers commonly promote a fullscreen video to
// the hardware video plane, and that plane is what does HDR passthrough and
// hardware scaling. Promotion usually needs the media element itself, so the
// mode is configurable: 'layer' keeps our controls, 'video' is the best shot
// at HDR but hands the UI over to the browser's native controls.
function fullscreenElement(){return document.fullscreenElement||document.webkitFullscreenElement||null;}
function requestFullscreen(el){if(!el)return false;var fn=el.requestFullscreen||el.webkitRequestFullscreen||el.webkitRequestFullScreen;if(!fn)return false;try{var r=fn.call(el);if(r&&r.catch)r.catch(function(e){KPApi.report('Fullscreen request rejected',{error:String(e&&e.message||e)},'media').catch(function(){});});return true;}catch(e){return false;}}
function exitFullscreen(){if(!fullscreenElement())return;var fn=document.exitFullscreen||document.webkitExitFullscreen||document.webkitCancelFullScreen;if(!fn)return;try{var r=fn.call(document);if(r&&r.catch)r.catch(function(){});}catch(e){}}
function playerFullscreenMode(){var mode=state.settings&&state.settings.player_fullscreen;return mode==='off'||mode==='video'?mode:'layer';}
// Must run inside the click that started playback: the user-activation window
// is gone by the time the play API call resolves.
function enterPlayerFullscreen(){var mode=playerFullscreenMode();if(mode==='off')return;if(fullscreenElement())return;requestFullscreen(mode==='video'?video:$('playerLayer'));}
function play(item,episode,startAt){enterPlayerFullscreen();state.playerResumePosition=Math.max(0,Number(startAt)||0);if(state.audioHlsJobId)stopAudioHlsJob(state.audioHlsJobId);if(state.audioHlsPendingJobId)stopAudioHlsJob(state.audioHlsPendingJobId);state.audioHlsJobId='';state.audioHlsPendingJobId='';if(!ensureSubscriptionForPlayback()){openSubscription();return;}state.audioHlsPollToken++;state.audioHlsPreparing=false;state.audioHlsActive=false;state.audioHlsOffset=0;state.audioHlsSelectedIndex=-1;state.playerAudioChoice='auto';state.playerOriginalDuration=0;state.hdrFellBack=false;state.current=item;$('playerTitle').textContent=item.title+(episode&&episode.title?' · '+episode.title:'');$('playerLayer').classList.remove('hidden');showPlayerControls();$('playerError').textContent='Получаем ссылку на видео…';var mediaId=episode&&(episode.media_id||episode.id);KPApi.play(item.id,mediaId).then(function(result){var st=result.selected||((result.streams||[])[0]);if(!st||!st.url)throw new Error('Ссылка на видео не найдена');preparePlayerOptions(result,st);$('playerError').textContent='';var group=currentQualityGroup(),preferred=preferredModeFor(group),initialUrl=streamUrlForGroup(group,preferred)||st.url;state.hdrAttempt=preferred==='direct'&&isHevcGroup(group);openUrl(initialUrl,preferred,(result.media&&result.media.id)||mediaId||'');}).catch(function(err){$('playerError').textContent='Не удалось получить поток: '+(err&&err.message?err.message:String(err));KPApi.report('Resolve stream failed',{item_id:item.id,error:String(err)},'media').catch(function(){});});}
function switchStreamMode(mode){if(!mode)return;var group=currentQualityGroup(),url=streamUrlForGroup(group,mode);if(!url){$('playerError').textContent='Для выбранного режима нет ссылки на поток.';return;}if(!state.audioHlsActive&&!state.audioHlsPreparing&&mode===state.mode)return;state.audioHlsPollToken++;state.audioHlsPreparing=false;if(state.audioHlsPendingJobId)stopAudioHlsJob(state.audioHlsPendingJobId);if(state.audioHlsJobId)stopAudioHlsJob(state.audioHlsJobId);state.audioHlsPendingJobId='';state.playerAudioChoice='auto';state.pendingAltAudioIndex=-1;state.altAudioUrl='';populateAudioMenu();var resume=currentResume();$('playerError').textContent='Переключаем поток…';openUrl(url,mode,state.episodeId,resume);clearSwitchMessage(resume);}
function switchQuality(index){var group=state.playerStreams[Number(index)];if(!group)return;state.playerQualityIndex=String(index);var mode=state.audioHlsActive?state.baseStreamMode:state.mode,url=streamUrlForGroup(group,mode);if(!url){$('playerError').textContent='Для этого качества нет подходящего потока.';return;}if(!state.audioHlsActive&&!state.audioHlsPreparing&&url===state.streamUrl)return;state.audioHlsPollToken++;state.audioHlsPreparing=false;if(state.audioHlsPendingJobId)stopAudioHlsJob(state.audioHlsPendingJobId);if(state.audioHlsJobId)stopAudioHlsJob(state.audioHlsJobId);state.audioHlsPendingJobId='';state.playerAudioChoice='auto';state.pendingAltAudioIndex=-1;state.altAudioUrl='';populateAudioMenu();var resume=currentResume();$('playerError').textContent='Меняем качество…';openUrl(url,mode,state.episodeId,resume);clearSwitchMessage(resume);}
function saveProgress(){if(!state.current)return;var savedPosition=Math.max(logicalCurrentTime(),Number(state.playerResumePosition)||0),totalDuration=logicalDuration();var completed=!!(totalDuration&&savedPosition/totalDuration>=.9);if(completed)state.watchedMap[String(state.current.id)]=1;else if(savedPosition>0&&state.watchedMap[String(state.current.id)]===undefined)state.watchedMap[String(state.current.id)]=0;KPApi.saveProgress({media_id:state.current.id,episode_id:state.episodeId||null,position:savedPosition,duration:totalDuration||0,completed:completed}).catch(function(){});}
function closePlayer(){exitFullscreen();saveProgress();state.audioHlsPollToken++;state.audioHlsPreparing=false;if(state.audioHlsPendingJobId)stopAudioHlsJob(state.audioHlsPendingJobId);if(state.audioHlsJobId)stopAudioHlsJob(state.audioHlsJobId);state.audioHlsPendingJobId='';state.audioHlsJobId='';video.pause();state.playerSwitching=false;destroyHls();video.removeAttribute('src');video.load();$('playerLayer').classList.add('hidden');$('playerLayer').classList.remove('controls-hidden');if(playerControlsTimer){clearTimeout(playerControlsTimer);playerControlsTimer=null;}}
// video.play() returns a promise that rejects with AbortError whenever the
// element is paused or reloaded before playback actually begins. That happens
// on every quality/audio/stream switch and when closing the player, and it is
// not a playback failure. Reporting it painted a real error over the UI and
// claimed a position had been saved.
function isAbortedPlay(error){var name=error&&error.name||'',message=String(error&&error.message||'');return name==='AbortError'||message.indexOf('interrupted')>=0;}
// seq ties the attempt to the stream switch that started it, so a rejection
// belonging to a stream we already navigated away from stays silent.
function startPlayback(seq){var promise=video.play();if(!promise||!promise.catch)return promise;promise.catch(function(error){if(isAbortedPlay(error))return;if(seq!==undefined&&seq!==state.streamSwitchSeq)return;if(error&&error.name==='NotAllowedError'){$('playerError').textContent='Браузер заблокировал автозапуск. Нажмите OK, чтобы начать воспроизведение.';keepPlayerControlsVisible();return;}mediaError(error&&error.message?error.message:String(error));});return promise;}
function fallbackFromDirect(){if(!state.hdrAttempt||state.mode!=='direct'||state.hdrFellBack)return false;var group=currentQualityGroup(),url=streamUrlForGroup(group,'hls')||streamUrlForGroup(group,'relay');if(!url)return false;state.hdrFellBack=true;state.hdrAttempt=false;var resume=currentResume();$('playerError').textContent='Прямой поток недоступен, переключаемся через сервер…';openUrl(url,group.variants&&group.variants.hls?'hls':'relay',state.episodeId,resume);clearSwitchMessage(resume);KPApi.report('Direct HDR playback failed, relayed instead',{quality:group&&group.quality,codec:group&&group.codec},'media').catch(function(){});return true;}
function mediaError(m){if(fallbackFromDirect())return;var failedPosition=Math.max(logicalCurrentTime(),Number(state.playerResumePosition)||0);if(failedPosition>0)state.playerResumePosition=failedPosition;state.playerSwitching=false;$('playerError').textContent=m+(state.playerResumePosition>0?' Позиция '+fmt(state.playerResumePosition)+' сохранена.':'');KPApi.report('Media error',{message:m,mode:state.mode,url:state.streamUrl,error:video.error&&video.error.code},'media').catch(function(){});}
function subscriptionDate(ts){if(!ts)return '';try{return new Date(Number(ts)*1000).toLocaleDateString('ru-RU',{day:'2-digit',month:'long',year:'numeric'});}catch(e){return '';}}
function subscriptionLabel(sub){var days=Number(sub&&sub.days);if(!sub||sub.expired||sub.active===false)return 'PRO истёк';if(!isFinite(days))return 'PRO';if(days<1)return Math.max(1,Math.ceil(days*24))+' ч.';return Math.max(1,Math.ceil(days))+' дн.';}
function applySubscription(profile){state.profile=profile||null;state.profileCheckedAt=Date.now();var chip=$('subscriptionChip');if(!chip)return;chip.classList.remove('subscription-loading','subscription-warning','subscription-critical','subscription-expired');var sub=profile&&profile.subscription||null;if(!sub){chip.textContent='—';chip.title='Не удалось определить подписку';return;}chip.textContent=subscriptionLabel(sub);var days=Number(sub.days);if(sub.expired||sub.active===false)chip.classList.add('subscription-expired');else if(isFinite(days)&&days<1)chip.classList.add('subscription-critical');else if(isFinite(days)&&days<=7)chip.classList.add('subscription-warning');chip.title=sub.expired?'Подписка закончилась':'Осталось '+chip.textContent;}
function loadProfile(refresh){var chip=$('subscriptionChip');if(chip){chip.classList.add('subscription-loading');if(!state.profile)chip.textContent='…';}return KPApi.profile(!!refresh).then(function(profile){applySubscription(profile);return profile;}).catch(function(){if(chip){chip.classList.remove('subscription-loading');if(!state.profile){chip.textContent='—';chip.title='Войдите или проверьте соединение';}}return null;});}
function openSubscription(){var profile=state.profile,sub=profile&&profile.subscription||{};var card=$('subscriptionModal').querySelector('.subscription-card');card.classList.remove('expired','warning');$('subscriptionTitle').textContent='Подписка '+appBrandName();$('subscriptionValue').textContent=subscriptionLabel(sub);var days=Number(sub.days);if(sub.expired||sub.active===false){card.classList.add('expired');$('subscriptionMessage').textContent='Подписка закончилась. Воспроизведение может быть недоступно. Продлите подписку в аккаунте KinoPub, затем нажмите «Обновить статус».';}else if(isFinite(days)&&days<=7){card.classList.add('warning');$('subscriptionMessage').textContent='Подписка скоро закончится. После продления статус обновится автоматически или по кнопке ниже.';}else{$('subscriptionMessage').textContent='Подписка активна.';}$('subscriptionEnd').textContent=sub.end_time?'Действует до '+subscriptionDate(sub.end_time):'';$('subscriptionModal').classList.remove('hidden');setTimeout(function(){$('subscriptionRefresh').focus();},20);}
function closeSubscription(){$('subscriptionModal').classList.add('hidden');}
function refreshSubscription(){var btn=$('subscriptionRefresh');btn.disabled=true;$('subscriptionValue').textContent='Проверяем…';loadProfile(true).then(function(){btn.disabled=false;openSubscription();});}
function ensureSubscriptionForPlayback(){var sub=state.profile&&state.profile.subscription;return !(sub&&(sub.expired||sub.active===false));}
function setAuthLocked(locked){state.authRequired=!!locked;document.body.classList.toggle('auth-locked',!!locked);$('authClose').classList.toggle('hidden',!!locked);}
function showAuthGate(){clearAuth();setAuthLocked(true);$('authModal').classList.remove('hidden');$('authIntro').classList.remove('hidden');$('authStart').classList.remove('hidden');$('authStart').disabled=false;$('authCodePanel').classList.add('hidden');$('authRestart').classList.add('hidden');$('verificationUri').textContent='';$('userCode').textContent='------';$('authCountdown').textContent='';$('authStatus').textContent='';setTimeout(function(){$('authStart').focus();},20);}
function loadWatchedStatuses(){return Promise.all([KPApi.watchingStatuses().catch(function(){return {statuses:{}};}),KPApi.history().catch(function(){return {items:[]};})]).then(function(results){var remote=(results[0]&&results[0].statuses)||{},local=(results[1]&&results[1].items)||[];state.watchedMap={};for(var id in remote)if(remote.hasOwnProperty(id))state.watchedMap[String(id)]=parseInt(remote[id],10);for(var i=0;i<local.length;i++){var row=local[i],mid=String(row.media_id||'');if(!mid)continue;if(row.completed)state.watchedMap[mid]=1;else if(state.watchedMap[mid]===undefined)state.watchedMap[mid]=0;}if(state.appInitialized&&state.route!=='settings')renderCatalog();return state.watchedMap;});}
function initializeAuthenticatedApp(){state.authenticated=true;setAuthLocked(false);$('authModal').classList.add('hidden');if(!state.appInitialized){state.appInitialized=true;applyHash();loadSettings();}else if(state.sessionExpired){state.sessionExpired=false;var scr=visibleScreen();if(scr==='detailsScreen'&&state.current)openDetails(state.current);else if(scr==='searchScreen')doSearch(state.searchMode);else renderCatalog();}loadProfile(true);loadWatchedStatuses();}
// A 401 from any authenticated call means the session died mid-use (cookie
// gone, or KinoPub's refresh token was revoked server-side) - show the exact
// same gate as a first-time visitor instead of leaving whatever error text
// the failed call's own .catch wrote into the page underneath it.
function handleSessionExpired(){if(state.authRequired)return;state.authenticated=false;state.sessionExpired=true;showAuthGate();}
function startAuth(){clearAuth();$('authModal').classList.remove('hidden');$('authIntro').classList.add('hidden');$('authStart').classList.add('hidden');$('authCodePanel').classList.remove('hidden');$('authRestart').classList.add('hidden');$('verificationUri').textContent='';$('userCode').textContent='------';$('authCountdown').textContent='';$('authStatus').textContent='Получаем код…';KPApi.startAuth().then(function(d){$('verificationUri').textContent=d.verification_uri;$('userCode').textContent=d.user_code;var left=d.expires_in||300;$('authStatus').textContent='Ожидаем подтверждение';$('authCountdown').textContent='Код действует ещё '+left+' сек.';state.authTick=setInterval(function(){left--;$('authCountdown').textContent='Код действует ещё '+left+' сек.';if(left<=0){clearAuth();$('authStatus').textContent='Код истёк';$('authRestart').classList.remove('hidden');$('authRestart').focus();}},500);state.authPoll=setInterval(function(){KPApi.pollAuth(d.code).then(function(x){if(x.status==='authorized'){clearAuth();$('authStatus').textContent='Устройство подключено';setTimeout(initializeAuthenticatedApp,700);}}).catch(function(e){KPApi.status().then(function(st){if(st&&st.authenticated){clearAuth();$('authStatus').textContent='Устройство подключено';setTimeout(initializeAuthenticatedApp,300);return;}clearAuth();$('authStatus').textContent='Ошибка: '+e.message;$('authRestart').classList.remove('hidden');}).catch(function(){clearAuth();$('authStatus').textContent='Ошибка: '+e.message;$('authRestart').classList.remove('hidden');});});},Math.max(5,d.interval||5)*1000);}).catch(function(e){$('authStatus').textContent='Ошибка: '+e.message;$('authRestart').classList.remove('hidden');});}
function clearAuth(){if(state.authTick)clearInterval(state.authTick);if(state.authPoll)clearInterval(state.authPoll);state.authTick=state.authPoll=null;}
function closeAuth(){if(state.authRequired)return;clearAuth();$('authModal').classList.add('hidden');}
function loadSettings(){KPApi.settings().then(function(s){state.settings=s;var icon=normalizeAppIcon(s.app_icon||'kinopub');state.settings.app_icon=icon;$('setQuality').value=s.quality;$('setMode').value=s.stream_mode;$('setAudio').value=s.audio_language;$('setSubs').value=s.subtitles;applySubtitleSize(s.subtitle_size);$('setFullscreen').value=playerFullscreenMode();$('setAutoplay').checked=!!s.autoplay_next;$('setMotion').checked=!!s.reduce_motion;$('setHistoryFrames').checked=s.history_episode_frames!==false;var radios=document.querySelectorAll('input[name="appIcon"]');for(var i=0;i<radios.length;i++)radios[i].checked=radios[i].value===icon;applyBranding(icon);document.body.classList.toggle('reduce-motion',!!s.reduce_motion);});}

function clearApplicationCache(){
 state.catalogCache={};
 state.catalogPages={};
 state.catalogTotals={};
 state.catalogRequest++;
 state.cacheVersion=Date.now();
 try{sessionStorage.clear();}catch(e){}
 try{localStorage.removeItem('kp_catalog_cache');}catch(e){}
 $('settingsStatus').textContent='Кэш каталога и обложек сброшен';
 KPApi.report('Frontend cache cleared',{cache_version:state.cacheVersion},'cache').catch(function(){});
 setTimeout(function(){route('popular');},350);
}
function saveSettings(){var s={quality:$('setQuality').value,stream_mode:$('setMode').value,audio_language:$('setAudio').value,subtitles:$('setSubs').value,subtitle_size:normalizeSubtitleSize($('setSubSize').value),player_fullscreen:$('setFullscreen').value,autoplay_next:$('setAutoplay').checked,reduce_motion:$('setMotion').checked,history_episode_frames:$('setHistoryFrames').checked,app_icon:selectedAppIcon()};KPApi.saveSettings(s).then(function(x){state.settings=x;x.app_icon=normalizeAppIcon(x.app_icon||'kinopub');applyBranding(x.app_icon);applySubtitleSize(x.subtitle_size);$('settingsStatus').textContent='Сохранено';document.body.classList.toggle('reduce-motion',!!x.reduce_motion);});}
function hideSuggestions(){var box=$('searchSuggestions');box.classList.add('hidden');box.innerHTML='';$('searchInput').setAttribute('aria-expanded','false');state.suggestionIndex=-1;}
function suggestionRows(){return $('searchSuggestions').querySelectorAll('.search-suggestion');}
function openSuggestion(item){if(!item)return;$('searchInput').value=item.value||'';hideSuggestions();if(item.id){state.current={id:String(item.id),title:item.value||''};openDetails(state.current);return;}doSearch('title',cleanSearchQuery(item.value||''));}
function setSuggestionIndex(index){var rows=suggestionRows();if(!rows.length){state.suggestionIndex=-1;return;}if(index<0)index=rows.length-1;if(index>=rows.length)index=0;state.suggestionIndex=index;for(var i=0;i<rows.length;i++)rows[i].classList.toggle('active',i===index);if(rows[index]){rows[index].scrollIntoView(false);$('searchInput').value=rows[index].getAttribute('data-value')||$('searchInput').value;}}
function renderSuggestions(items){var box=$('searchSuggestions');box.innerHTML='';state.suggestionIndex=-1;state.currentSuggestions=items||[];if(!items||!items.length){box.innerHTML='<div class="search-suggestion-empty">Ничего не найдено…</div>';box.classList.remove('hidden');$('searchInput').setAttribute('aria-expanded','true');return;}for(var i=0;i<items.length;i++){var item=items[i],row=document.createElement('button');row.type='button';row.className='search-suggestion';row.setAttribute('role','option');row.setAttribute('data-id',item.id||'');row.setAttribute('data-value',item.value||'');row.innerHTML=highlightSuggestion(item.value||'', $('searchInput').value||'');row.onclick=(function(x){return function(){openSuggestion(x);};}(item));box.appendChild(row);}box.classList.remove('hidden');$('searchInput').setAttribute('aria-expanded','true');}
function highlightSuggestion(value,query){var safe=esc(value),q=String(query||'').trim();if(!q)return safe;var pos=String(value).toLowerCase().indexOf(q.toLowerCase());if(pos<0)return safe;return esc(String(value).slice(0,pos))+'<strong>'+esc(String(value).slice(pos,pos+q.length))+'</strong>'+esc(String(value).slice(pos+q.length));}
function cleanSearchQuery(value){return String(value||'').replace(/\s*\(\d{4}\)\s*$/,'').replace(/\s*\/.*$/,'').trim();}
function requestSuggestions(){var q=$('searchInput').value.trim();if(state.searchTimer)clearTimeout(state.searchTimer);if(q.length<2){state.currentSuggestions=[];hideSuggestions();return;}state.searchTimer=setTimeout(function(){var seq=++state.searchSeq;KPApi.autocomplete(q).then(function(d){if(seq!==state.searchSeq||$('searchInput').value.trim()!==q)return;renderSuggestions((d&&d.items)||[]);}).catch(function(){if(seq===state.searchSeq)hideSuggestions();});},220);}
function doSearch(mode,forcedQuery){var raw=forcedQuery!==undefined?forcedQuery:$('searchInput').value.trim();var q=cleanSearchQuery(raw);if(!q)return;hideSuggestions();if(typeof mode==='string')state.searchMode=mode;$('searchInput').value=q;showScreen('searchScreen');$('searchPageTitle').textContent='Поиск: '+q;pushHash(encodeSearchHash(state.searchMode,q));var modeButtons=document.querySelectorAll('[data-search-mode]');for(var m=0;m<modeButtons.length;m++)modeButtons[m].classList.toggle('active',modeButtons[m].getAttribute('data-search-mode')===state.searchMode);$('searchStatus').textContent='Ищем…';var g=$('searchResults');g.innerHTML='';KPApi.search(q,state.searchMode).then(function(d){var items=d.items||[];$('searchStatus').textContent=items.length?'Найдено: '+items.length:'';for(var i=0;i<items.length;i++)g.appendChild(card(items[i]));if(!items.length)g.innerHTML='<p class="empty-state">Ничего не найдено</p>';}).catch(function(e){$('searchStatus').textContent='Ошибка поиска';g.innerHTML='<p class="empty-state">'+esc(e&&e.message?e.message:String(e))+'</p>';});}
function diagRow(k,v){return'<div class="diag-key">'+esc(k)+'</div><div class="diag-value">'+esc(v)+'</div>';}
function openDiag(){var v=document.createElement('video'),caps=mediaCapabilities(),group=currentQualityGroup(),d={userAgent:navigator.userAgent,screen:screen.width+'×'+screen.height,hls:v.canPlayType('application/vnd.apple.mpegurl')||'нет','H.264':caps.h264.answer+(caps.h264.mse?' + MSE':''),'HEVC (Main10)':caps.hevc.answer+(caps.hevc.mse?' + MSE':'')+(caps.hevc.native?'':' — аппаратно недоступен'),'HDR-дисплей':caps.hdrDisplay?'да':'не сообщает','Гамут P3':caps.wideGamut?'да':'не сообщает','Потолок качества':qualityCap()?qualityCap()+'p':'авто','Текущий вариант':group?((group.quality||group.height+'p')+' · '+(group.codec||'?')+' · '+state.mode.toUpperCase()):'плеер закрыт','Декодируется':video.videoWidth?(video.videoWidth+'×'+video.videoHeight):'нет потока','Полный экран':playerFullscreenMode()+(fullscreenElement()?' · активен':' · не активен')};var h='';for(var k in d)h+=diagRow(k,d[k]);$('diagnosticsGrid').innerHTML=h;KPApi.health().then(function(x){$('diagnosticsGrid').innerHTML+=diagRow('backend',x.status+' '+x.version)+diagRow('API credentials',x.credentials_configured?'настроены':'не настроены');});KPApi.status().then(function(x){$('diagnosticsGrid').innerHTML+=diagRow('Сессия',x.authenticated?'активна':'нет')+diagRow('Refresh-токен',x.authenticated?(x.has_refresh_token?'есть':'ОТСУТСТВУЕТ — сессия умрёт вместе с access-токеном'):'—')+diagRow('Токен истекает через',x.authenticated?fmt(x.expires_in||0):'—');});KPApi.debugEvents().then(function(x){var l=[],a=x.events||[];for(var i=0;i<Math.min(a.length,30);i++)l.push(new Date(a[i].at*1000).toLocaleTimeString()+' ['+a[i].kind+'] '+a[i].message);$('diagnosticsLog').textContent=l.join('\n')||'Логи пусты';});$('diagnosticsModal').classList.remove('hidden');}
function openExplorer(){$('apiExplorerModal').classList.remove('hidden');setTimeout(function(){$('explorerPath').focus();},0);}function closeExplorer(){$('apiExplorerModal').classList.add('hidden');}
function runExplorer(){var p=$('explorerPath').value.trim(),q=$('explorerQuery').value.trim();if(!p)return;KPApi.explore(p,q).then(function(d){$('explorerStatus').textContent='HTTP '+(d.response?d.response.status:'?');$('explorerOutput').textContent=JSON.stringify(d,null,2);}).catch(function(e){$('explorerStatus').textContent='Ошибка';$('explorerOutput').textContent=e.message;});}
function compareFeeds(){
 $('explorerStatus').textContent='Сравниваем варианты сортировки…';$('explorerOutput').textContent='Выполняется до 9 API-запросов. Подождите…';
 KPApi.compareFeeds('both').then(function(d){
   var lines=[];
   lines.push('Снимок сайта: '+(d.snapshot||''));
   if(d.best&&d.best.popular)lines.push('Лучший для Популярных: '+d.best.popular.candidate+' — '+d.best.popular.score+'%, совпадений '+d.best.popular.overlap+'/'+d.best.popular.target_count+', точных позиций '+d.best.popular.exact_positions);
   if(d.best&&d.best.hot)lines.push('Лучший для Горячих: '+d.best.hot.candidate+' — '+d.best.hot.score+'%, совпадений '+d.best.hot.overlap+'/'+d.best.hot.target_count+', точных позиций '+d.best.hot.exact_positions);
   $('explorerStatus').textContent=lines.join(' · ');
   $('explorerOutput').textContent=JSON.stringify(d,null,2);
 }).catch(function(e){$('explorerStatus').textContent='Ошибка сравнения';$('explorerOutput').textContent=e.message;});
}
function downloadExplorer(){window.location.href=KPApi.explorerDownloadUrl($('explorerPath').value.trim(),$('explorerQuery').value.trim());}
function copyExplorer(){var t=$('explorerOutput').textContent||'',ta=document.createElement('textarea');ta.value=t;document.body.appendChild(ta);ta.select();try{document.execCommand('copy');$('explorerStatus').textContent='Ответ скопирован';}catch(e){}document.body.removeChild(ta);}
function test(mode){var u=$('testUrl').value.trim();if(!u)return;state.current={id:'diagnostic',title:'Тестовый поток',streams:[{url:u}]};$('diagnosticsModal').classList.add('hidden');$('playerLayer').classList.remove('hidden');showPlayerControls();$('playerTitle').textContent='Тестовый поток';openUrl(u,mode,'');}
function move(dir){var a=visibleFocus(),cur=document.activeElement;if(a.indexOf(cur)<0){focusFirst();return;}var r=cur.getBoundingClientRect(),best=null,score=1e9;for(var i=0;i<a.length;i++){if(a[i]===cur)continue;var b=a[i].getBoundingClientRect(),dx=b.left+b.width/2-r.left-r.width/2,dy=b.top+b.height/2-r.top-r.height/2;if(dir==='left'&&dx>=-5||dir==='right'&&dx<=5||dir==='up'&&dy>=-5||dir==='down'&&dy<=5)continue;var p=(dir==='left'||dir==='right')?Math.abs(dx):Math.abs(dy),q=(dir==='left'||dir==='right')?Math.abs(dy):Math.abs(dx),s=p+q*3;if(s<score){score=s;best=a[i];}}if(best){best.focus();try{best.scrollIntoView(false);}catch(e){}}}
document.onkeydown=function(e){var k=e.keyCode;if(k>=37&&k<=40){e.preventDefault();move(k===37?'left':k===38?'up':k===39?'right':'down');}else if(k===13&&document.activeElement&&document.activeElement.click){e.preventDefault();document.activeElement.click();}else if(k===461||k===27||k===10009){e.preventDefault();if(!$('playerLayer').classList.contains('hidden'))closePlayer();else if(!$('apiExplorerModal').classList.contains('hidden'))closeExplorer();else if(!$('diagnosticsModal').classList.contains('hidden'))$('diagnosticsModal').classList.add('hidden');else if(!$('authModal').classList.contains('hidden'))closeAuth();else if(!$('subscriptionModal').classList.contains('hidden'))closeSubscription();else if(!$('detailsScreen').classList.contains('hidden'))history.back();else route('popular');}};
video.ontimeupdate=updatePlayerProgress;function updatePlayButton(){var b=$('togglePlay');if(!b)return;var paused=video.paused;b.setAttribute('aria-label',paused?'Воспроизвести':'Пауза');b.title=paused?'Воспроизвести':'Пауза';b.innerHTML=paused?'<svg class="icon-play" viewBox="0 0 48 48" aria-hidden="true"><path d="M17 11l22 13-22 13z"/></svg>':'<svg class="icon-pause" viewBox="0 0 48 48" aria-hidden="true"><rect x="14" y="11" width="7" height="26" rx="2"/><rect x="27" y="11" width="7" height="26" rx="2"/></svg>';};video.onplay=function(){updatePlayButton();};video.onpause=function(){updatePlayButton();saveProgress();};video.onerror=function(){var c={1:'Воспроизведение прервано',2:'Сетевая ошибка',3:'Ошибка декодирования',4:'Формат не поддерживается'};mediaError(c[video.error&&video.error.code]||'Ошибка видео');};video.onended=function(){updatePlayButton();saveProgress();if(state.route!=='settings')renderCatalog();};
var links=document.querySelectorAll('[data-route]');for(var i=0;i<links.length;i++)links[i].onclick=(function(r){return function(){route(r);};}(links[i].getAttribute('data-route')));
var iconRadios=document.querySelectorAll('input[name="appIcon"]');for(var ir=0;ir<iconRadios.length;ir++)iconRadios[ir].onchange=function(){applyBranding(this.value);};$('searchGo').onclick=function(){doSearch();};$('searchInput').oninput=requestSuggestions;$('searchInput').onfocus=requestSuggestions;$('searchInput').onkeydown=function(e){var k=e.keyCode;if(k===40&&!$('searchSuggestions').classList.contains('hidden')){e.preventDefault();e.stopPropagation();setSuggestionIndex(state.suggestionIndex+1);return;}if(k===38&&!$('searchSuggestions').classList.contains('hidden')){e.preventDefault();e.stopPropagation();setSuggestionIndex(state.suggestionIndex-1);return;}if(k===27){e.stopPropagation();hideSuggestions();return;}if(k===13){e.preventDefault();e.stopPropagation();var rows=suggestionRows();if(state.suggestionIndex>=0&&state.currentSuggestions[state.suggestionIndex]){openSuggestion(state.currentSuggestions[state.suggestionIndex]);return;}var value=$('searchInput').value.trim(),exact=null;for(var si=0;si<state.currentSuggestions.length;si++){if((state.currentSuggestions[si].value||'').trim()===value){exact=state.currentSuggestions[si];break;}}if(exact){openSuggestion(exact);}else doSearch();}};var searchModes=document.querySelectorAll('[data-search-mode]');for(var sm=0;sm<searchModes.length;sm++)searchModes[sm].onclick=(function(mode){return function(){state.searchMode=mode;doSearch(mode);};}(searchModes[sm].getAttribute('data-search-mode')));document.addEventListener('click',function(e){if(!$('searchHead').contains(e.target))hideSuggestions();});$('loginButton').onclick=function(){if(!state.authenticated)showAuthGate();};$('authStart').onclick=startAuth;$('subscriptionChip').onclick=openSubscription;$('subscriptionClose').onclick=closeSubscription;$('subscriptionRefresh').onclick=refreshSubscription;$('authClose').onclick=closeAuth;$('authRestart').onclick=startAuth;$('detailsClose').onclick=function(){history.back();};$('saveSettings').onclick=saveSettings;$('clearCache').onclick=clearApplicationCache;$('diagnosticsButton').onclick=openDiag;$('diagnosticsClose').onclick=function(){$('diagnosticsModal').classList.add('hidden');};$('apiExplorerButton').onclick=openExplorer;$('apiExplorerClose').onclick=closeExplorer;$('explorerRun').onclick=runExplorer;$('compareFeeds').onclick=compareFeeds;$('explorerDownload').onclick=downloadExplorer;$('explorerCopy').onclick=copyExplorer;$('testDirect').onclick=function(){test('direct');};$('testRelay').onclick=function(){test('relay');};$('testHls').onclick=function(){test('hls');};var playbackFlashTimer=null,playerControlsTimer=null;
function showPlayerControls(){var layer=$('playerLayer');if(!layer)return;layer.classList.remove('controls-hidden');if(playerControlsTimer){clearTimeout(playerControlsTimer);playerControlsTimer=null;}if(!video.paused&&!video.ended){playerControlsTimer=setTimeout(function(){if(!video.paused&&!video.ended&&!layer.classList.contains('hidden'))layer.classList.add('controls-hidden');},3000);}}
function keepPlayerControlsVisible(){var layer=$('playerLayer');if(layer)layer.classList.remove('controls-hidden');if(playerControlsTimer){clearTimeout(playerControlsTimer);playerControlsTimer=null;}}
function showPlaybackFlash(kind){var flash=$('playbackFlash'),icon=$('playbackFlashIcon');if(!flash||!icon)return;if(playbackFlashTimer)clearTimeout(playbackFlashTimer);flash.classList.remove('show');icon.className='playback-flash-icon '+kind;void flash.offsetWidth;flash.classList.add('show');playbackFlashTimer=setTimeout(function(){flash.classList.remove('show');},500);}
function toggleVideoPlayback(showFlash){if(video.paused){startPlayback();if(showFlash!==false)showPlaybackFlash('play');}else{video.pause();if(showFlash!==false)showPlaybackFlash('pause');}}
function seekLogical(target){target=Math.max(0,Math.min(logicalDuration()||Infinity,target));if(state.audioHlsActive&&!audioHlsCanSeek(target)){var selected=state.audioHlsSelectedIndex>=0?state.audioHlsSelectedIndex:Number(String(state.playerAudioChoice).split(':')[1]),paused=video.paused||video.ended;if(isFinite(selected)&&selected>=0){prepareAudioHls(selected,{position:target,paused:paused},state.playerAudioChoice);return;}}var local=state.audioHlsActive?Math.max(0,target-Number(state.audioHlsOffset||0)):target;try{video.currentTime=local;}catch(e){}state.playerResumePosition=target;updatePlayerProgress();}
// The remote's OK key reaches a focused element through activeElement.click(),
// which produces a synthetic event with clientX = 0. Treating that as a click
// at the far left of the bar sent the video back to 00:00. Real pointer clicks
// report detail >= 1, so a synthetic one toggles playback instead of seeking.
function seekFromTimelineEvent(e){if(!e||!e.detail){toggleVideoPlayback();return;}var duration=logicalDuration();if(!isFinite(duration)||duration<=0)return;var rect=$('timeline').getBoundingClientRect();if(!rect.width)return;var x=Math.max(0,Math.min(rect.width,e.clientX-rect.left));seekLogical((x/rect.width)*duration);}
function updatePlayerProgress(){var current=logicalCurrentTime(),duration=logicalDuration();if(current>0&&!state.playerSwitching)state.playerResumePosition=current;var ratio=(duration&&isFinite(duration))?Math.max(0,Math.min(1,current/duration)):0;$('progress').style.width=(ratio*100)+'%';$('timeline').setAttribute('aria-valuenow',String(Math.round(ratio*100)));$('currentTime').textContent=fmt(current);$('duration').textContent=fmt(duration);}
$('playerLayer').onclick=function(e){if(e.target.closest&&e.target.closest('.player-controls, .timeline, .player-close-x'))return;toggleVideoPlayback();showPlayerControls();};
$('timeline').onclick=function(e){e.stopPropagation();seekFromTimelineEvent(e);};
$('timeline').onkeydown=function(e){if(e.keyCode===37||e.keyCode===39){e.preventDefault();e.stopPropagation();var step=Math.max(5,(logicalDuration()||0)*0.01);seekLogical(logicalCurrentTime()+(e.keyCode===37?-step:step));}};
$('togglePlay').onclick=function(e){e.stopPropagation();toggleVideoPlayback(false);showPlayerControls();};$('rewind').onclick=function(){seekLogical(logicalCurrentTime()-10);showPlayerControls();};$('forward').onclick=function(){seekLogical(logicalCurrentTime()+10);showPlayerControls();};$('playerStreamMode').onchange=function(e){e.stopPropagation();switchStreamMode(this.value);showPlayerControls();};$('playerQuality').onchange=function(e){e.stopPropagation();switchQuality(this.value);showPlayerControls();};$('playerSubtitles').onchange=function(e){e.stopPropagation();applySubtitleChoice(this.value);showPlayerControls();};$('playerSubtitleSize').onchange=function(e){e.stopPropagation();var size=applySubtitleSize(this.value);showPlayerControls();var next={};for(var k in state.settings)if(state.settings.hasOwnProperty(k))next[k]=state.settings[k];next.subtitle_size=size;KPApi.saveSettings(next).catch(function(){});};$('playerAudio').onchange=function(e){e.stopPropagation();applyAudioChoice(this.value);showPlayerControls();};$('closePlayer').onclick=closePlayer;$('playerCloseX').onclick=function(e){e.stopPropagation();closePlayer();};$('playerLayer').onmousemove=showPlayerControls;$('playerLayer').onmouseenter=showPlayerControls;function refreshNativeTracks(){populateAudioMenu();populateSubtitleMenu();reapplyAudioSelection();applySubtitleChoice(state.playerSubtitleChoice);}video.addEventListener('loadedmetadata',refreshNativeTracks);video.addEventListener('loadeddata',refreshNativeTracks);video.addEventListener('canplay',refreshNativeTracks);if(video.audioTracks){video.audioTracks.onaddtrack=refreshNativeTracks;video.audioTracks.onchange=function(){populateAudioMenu();};}if(video.textTracks){video.textTracks.onaddtrack=refreshNativeTracks;video.textTracks.onchange=function(){populateSubtitleMenu();};}video.addEventListener('play',showPlayerControls);video.addEventListener('pause',keepPlayerControlsVisible);video.addEventListener('ended',keepPlayerControlsVisible);
document.addEventListener('fullscreenchange',function(){keepPlayerControlsVisible();});document.addEventListener('webkitfullscreenchange',function(){keepPlayerControlsVisible();});
if(typeof window!=='undefined'&&window.addEventListener)window.addEventListener('hashchange',applyHash);
// The sidebar has its own overflow:auto (its nav list can outgrow the
// window on a short screen), so a wheel scroll started over it scrolled the
// sidebar instead of the content the user is actually looking at. Redirect
// it to whichever screen is currently visible instead.
var sidebarEl=document.querySelector('.sidebar');
if(sidebarEl)sidebarEl.addEventListener('wheel',function(e){var target=$(visibleScreen());if(!target)return;e.preventDefault();target.scrollTop+=e.deltaY;},{passive:false});
document.body.classList.add('auth-locked');KPApi.status().then(function(s){state.authenticated=!!s.authenticated;$('loginButton').title=s.authenticated?'Профиль':'Войти';if(s.authenticated)initializeAuthenticatedApp();else{applySubscription(null);showAuthGate();}}).catch(function(){applySubscription(null);showAuthGate();});setInterval(function(){if(state.authenticated)loadProfile(false);},6*60*60*1000);document.addEventListener('visibilitychange',function(){if(!document.hidden&&state.authenticated&&Date.now()-state.profileCheckedAt>30*60*1000)loadProfile(false);});
}());
