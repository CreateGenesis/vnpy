var ut=typeof globalThis<"u"?globalThis:typeof window<"u"?window:typeof global<"u"?global:typeof self<"u"?self:{};function J(p){return p&&p.__esModule&&Object.prototype.hasOwnProperty.call(p,"default")?p.default:p}var O={exports:{}},o={};/**
 * @license React
 * react.production.js
 *
 * Copyright (c) Meta Platforms, Inc. and affiliates.
 *
 * This source code is licensed under the MIT license found in the
 * LICENSE file in the root directory of this source tree.
 */var D;function tt(){if(D)return o;D=1;var p=Symbol.for("react.transitional.element"),l=Symbol.for("react.portal"),y=Symbol.for("react.fragment"),h=Symbol.for("react.strict_mode"),m=Symbol.for("react.profiler"),v=Symbol.for("react.consumer"),g=Symbol.for("react.context"),w=Symbol.for("react.forward_ref"),T=Symbol.for("react.suspense"),M=Symbol.for("react.memo"),C=Symbol.for("react.lazy"),b=Symbol.iterator;function B(t){return t===null||typeof t!="object"?null:(t=b&&t[b]||t["@@iterator"],typeof t=="function"?t:null)}var P={isMounted:function(){return!1},enqueueForceUpdate:function(){},enqueueReplaceState:function(){},enqueueSetState:function(){}},$=Object.assign,L={};function k(t,e,r){this.props=t,this.context=e,this.refs=L,this.updater=r||P}k.prototype.isReactComponent={},k.prototype.setState=function(t,e){if(typeof t!="object"&&typeof t!="function"&&t!=null)throw Error("takes an object of state variables to update or a function which returns an object of state variables.");this.updater.enqueueSetState(this,t,e,"setState")},k.prototype.forceUpdate=function(t){this.updater.enqueueForceUpdate(this,t,"forceUpdate")};function N(){}N.prototype=k.prototype;function A(t,e,r){this.props=t,this.context=e,this.refs=L,this.updater=r||P}var S=A.prototype=new N;S.constructor=A,$(S,k.prototype),S.isPureReactComponent=!0;var q=Array.isArray,i={H:null,A:null,T:null,S:null},z=Object.prototype.hasOwnProperty;function j(t,e,r,n,s,c){return r=c.ref,{$$typeof:p,type:t,key:e,ref:r!==void 0?r:null,props:c}}function K(t,e){return j(t.type,e,void 0,void 0,void 0,t.props)}function H(t){return typeof t=="object"&&t!==null&&t.$$typeof===p}function X(t){var e={"=":"=0",":":"=2"};return"$"+t.replace(/[=:]/g,function(r){return e[r]})}var Y=/\/+/g;function x(t,e){return typeof t=="object"&&t!==null&&t.key!=null?X(""+t.key):e.toString(36)}function I(){}function Z(t){switch(t.status){case"fulfilled":return t.value;case"rejected":throw t.reason;default:switch(typeof t.status=="string"?t.then(I,I):(t.status="pending",t.then(function(e){t.status==="pending"&&(t.status="fulfilled",t.value=e)},function(e){t.status==="pending"&&(t.status="rejected",t.reason=e)})),t.status){case"fulfilled":return t.value;case"rejected":throw t.reason}}throw t}function _(t,e,r,n,s){var c=typeof t;(c==="undefined"||c==="boolean")&&(t=null);var u=!1;if(t===null)u=!0;else switch(c){case"bigint":case"string":case"number":u=!0;break;case"object":switch(t.$$typeof){case p:case l:u=!0;break;case C:return u=t._init,_(u(t._payload),e,r,n,s)}}if(u)return s=s(t),u=n===""?"."+x(t,0):n,q(s)?(r="",u!=null&&(r=u.replace(Y,"$&/")+"/"),_(s,e,r,"",function(F){return F})):s!=null&&(H(s)&&(s=K(s,r+(s.key==null||t&&t.key===s.key?"":(""+s.key).replace(Y,"$&/")+"/")+u)),e.push(s)),1;u=0;var d=n===""?".":n+":";if(q(t))for(var a=0;a<t.length;a++)n=t[a],c=d+x(n,a),u+=_(n,e,r,c,s);else if(a=B(t),typeof a=="function")for(t=a.call(t),a=0;!(n=t.next()).done;)n=n.value,c=d+x(n,a++),u+=_(n,e,r,c,s);else if(c==="object"){if(typeof t.then=="function")return _(Z(t),e,r,n,s);throw e=String(t),Error("Objects are not valid as a React child (found: "+(e==="[object Object]"?"object with keys {"+Object.keys(t).join(", ")+"}":e)+"). If you meant to render a collection of children, use an array instead.")}return u}function R(t,e,r){if(t==null)return t;var n=[],s=0;return _(t,n,"","",function(c){return e.call(r,c,s++)}),n}function Q(t){if(t._status===-1){var e=t._result;e=e(),e.then(function(r){(t._status===0||t._status===-1)&&(t._status=1,t._result=r)},function(r){(t._status===0||t._status===-1)&&(t._status=2,t._result=r)}),t._status===-1&&(t._status=0,t._result=e)}if(t._status===1)return t._result.default;throw t._result}var U=typeof reportError=="function"?reportError:function(t){if(typeof window=="object"&&typeof window.ErrorEvent=="function"){var e=new window.ErrorEvent("error",{bubbles:!0,cancelable:!0,message:typeof t=="object"&&t!==null&&typeof t.message=="string"?String(t.message):String(t),error:t});if(!window.dispatchEvent(e))return}else if(typeof process=="object"&&typeof process.emit=="function"){process.emit("uncaughtException",t);return}console.error(t)};function V(){}return o.Children={map:R,forEach:function(t,e,r){R(t,function(){e.apply(this,arguments)},r)},count:function(t){var e=0;return R(t,function(){e++}),e},toArray:function(t){return R(t,function(e){return e})||[]},only:function(t){if(!H(t))throw Error("React.Children.only expected to receive a single React element child.");return t}},o.Component=k,o.Fragment=y,o.Profiler=m,o.PureComponent=A,o.StrictMode=h,o.Suspense=T,o.__CLIENT_INTERNALS_DO_NOT_USE_OR_WARN_USERS_THEY_CANNOT_UPGRADE=i,o.act=function(){throw Error("act(...) is not supported in production builds of React.")},o.cache=function(t){return function(){return t.apply(null,arguments)}},o.cloneElement=function(t,e,r){if(t==null)throw Error("The argument must be a React element, but you passed "+t+".");var n=$({},t.props),s=t.key,c=void 0;if(e!=null)for(u in e.ref!==void 0&&(c=void 0),e.key!==void 0&&(s=""+e.key),e)!z.call(e,u)||u==="key"||u==="__self"||u==="__source"||u==="ref"&&e.ref===void 0||(n[u]=e[u]);var u=arguments.length-2;if(u===1)n.children=r;else if(1<u){for(var d=Array(u),a=0;a<u;a++)d[a]=arguments[a+2];n.children=d}return j(t.type,s,void 0,void 0,c,n)},o.createContext=function(t){return t={$$typeof:g,_currentValue:t,_currentValue2:t,_threadCount:0,Provider:null,Consumer:null},t.Provider=t,t.Consumer={$$typeof:v,_context:t},t},o.createElement=function(t,e,r){var n,s={},c=null;if(e!=null)for(n in e.key!==void 0&&(c=""+e.key),e)z.call(e,n)&&n!=="key"&&n!=="__self"&&n!=="__source"&&(s[n]=e[n]);var u=arguments.length-2;if(u===1)s.children=r;else if(1<u){for(var d=Array(u),a=0;a<u;a++)d[a]=arguments[a+2];s.children=d}if(t&&t.defaultProps)for(n in u=t.defaultProps,u)s[n]===void 0&&(s[n]=u[n]);return j(t,c,void 0,void 0,null,s)},o.createRef=function(){return{current:null}},o.forwardRef=function(t){return{$$typeof:w,render:t}},o.isValidElement=H,o.lazy=function(t){return{$$typeof:C,_payload:{_status:-1,_result:t},_init:Q}},o.memo=function(t,e){return{$$typeof:M,type:t,compare:e===void 0?null:e}},o.startTransition=function(t){var e=i.T,r={};i.T=r;try{var n=t(),s=i.S;s!==null&&s(r,n),typeof n=="object"&&n!==null&&typeof n.then=="function"&&n.then(V,U)}catch(c){U(c)}finally{i.T=e}},o.unstable_useCacheRefresh=function(){return i.H.useCacheRefresh()},o.use=function(t){return i.H.use(t)},o.useActionState=function(t,e,r){return i.H.useActionState(t,e,r)},o.useCallback=function(t,e){return i.H.useCallback(t,e)},o.useContext=function(t){return i.H.useContext(t)},o.useDebugValue=function(){},o.useDeferredValue=function(t,e){return i.H.useDeferredValue(t,e)},o.useEffect=function(t,e){return i.H.useEffect(t,e)},o.useId=function(){return i.H.useId()},o.useImperativeHandle=function(t,e,r){return i.H.useImperativeHandle(t,e,r)},o.useInsertionEffect=function(t,e){return i.H.useInsertionEffect(t,e)},o.useLayoutEffect=function(t,e){return i.H.useLayoutEffect(t,e)},o.useMemo=function(t,e){return i.H.useMemo(t,e)},o.useOptimistic=function(t,e){return i.H.useOptimistic(t,e)},o.useReducer=function(t,e,r){return i.H.useReducer(t,e,r)},o.useRef=function(t){return i.H.useRef(t)},o.useState=function(t){return i.H.useState(t)},o.useSyncExternalStore=function(t,e,r){return i.H.useSyncExternalStore(t,e,r)},o.useTransition=function(){return i.H.useTransition()},o.version="19.0.0",o}var G;function et(){return G||(G=1,O.exports=tt()),O.exports}var E=et();const st=J(E);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const rt=p=>p.replace(/([a-z0-9])([A-Z])/g,"$1-$2").toLowerCase(),W=(...p)=>p.filter((l,y,h)=>!!l&&l.trim()!==""&&h.indexOf(l)===y).join(" ").trim();/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */var nt={xmlns:"http://www.w3.org/2000/svg",width:24,height:24,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2,strokeLinecap:"round",strokeLinejoin:"round"};/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const ot=E.forwardRef(({color:p="currentColor",size:l=24,strokeWidth:y=2,absoluteStrokeWidth:h,className:m="",children:v,iconNode:g,...w},T)=>E.createElement("svg",{ref:T,...nt,width:l,height:l,stroke:p,strokeWidth:h?Number(y)*24/Number(l):y,className:W("lucide",m),...w},[...g.map(([M,C])=>E.createElement(M,C)),...Array.isArray(v)?v:[v]]));/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const f=(p,l)=>{const y=E.forwardRef(({className:h,...m},v)=>E.createElement(ot,{ref:v,iconNode:l,className:W(`lucide-${rt(p)}`,h),...m}));return y.displayName=`${p}`,y};/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const it=f("Activity",[["path",{d:"M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2",key:"169zse"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const ct=f("Check",[["path",{d:"M20 6 9 17l-5-5",key:"1gmf2c"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const at=f("CircleCheck",[["circle",{cx:"12",cy:"12",r:"10",key:"1mglay"}],["path",{d:"m9 12 2 2 4-4",key:"dzmm74"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const ft=f("Clock3",[["circle",{cx:"12",cy:"12",r:"10",key:"1mglay"}],["polyline",{points:"12 6 12 12 16.5 12",key:"1aq6pp"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const pt=f("FileCheck2",[["path",{d:"M4 22h14a2 2 0 0 0 2-2V7l-5-5H6a2 2 0 0 0-2 2v4",key:"1pf5j1"}],["path",{d:"M14 2v4a2 2 0 0 0 2 2h4",key:"tnqrlb"}],["path",{d:"m3 15 2 2 4-4",key:"1lhrkk"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const lt=f("FileLock2",[["path",{d:"M4 22h14a2 2 0 0 0 2-2V7l-5-5H6a2 2 0 0 0-2 2v1",key:"jmtmu2"}],["path",{d:"M14 2v4a2 2 0 0 0 2 2h4",key:"tnqrlb"}],["rect",{width:"8",height:"5",x:"2",y:"13",rx:"1",key:"10y5wo"}],["path",{d:"M8 13v-2a2 2 0 1 0-4 0v2",key:"1pdxzg"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const yt=f("Gauge",[["path",{d:"m12 14 4-4",key:"9kzdfg"}],["path",{d:"M3.34 19a10 10 0 1 1 17.32 0",key:"19p75a"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const dt=f("History",[["path",{d:"M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8",key:"1357e3"}],["path",{d:"M3 3v5h5",key:"1xhq8a"}],["path",{d:"M12 7v5l4 2",key:"1fdv2h"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const ht=f("MessageSquareText",[["path",{d:"M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",key:"1lielz"}],["path",{d:"M13 8H7",key:"14i4kc"}],["path",{d:"M17 12H7",key:"16if0g"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const vt=f("Octagon",[["path",{d:"M2.586 16.726A2 2 0 0 1 2 15.312V8.688a2 2 0 0 1 .586-1.414l4.688-4.688A2 2 0 0 1 8.688 2h6.624a2 2 0 0 1 1.414.586l4.688 4.688A2 2 0 0 1 22 8.688v6.624a2 2 0 0 1-.586 1.414l-4.688 4.688a2 2 0 0 1-1.414.586H8.688a2 2 0 0 1-1.414-.586z",key:"2d38gg"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const kt=f("Pause",[["rect",{x:"14",y:"4",width:"4",height:"16",rx:"1",key:"zuxfzm"}],["rect",{x:"6",y:"4",width:"4",height:"16",rx:"1",key:"1okwgv"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const _t=f("Play",[["polygon",{points:"6 3 20 12 6 21 6 3",key:"1oa8hb"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const Et=f("Radio",[["path",{d:"M4.9 19.1C1 15.2 1 8.8 4.9 4.9",key:"1vaf9d"}],["path",{d:"M7.8 16.2c-2.3-2.3-2.3-6.1 0-8.5",key:"u1ii0m"}],["circle",{cx:"12",cy:"12",r:"2",key:"1c9p78"}],["path",{d:"M16.2 7.8c2.3 2.3 2.3 6.1 0 8.5",key:"1j5fej"}],["path",{d:"M19.1 4.9C23 8.8 23 15.1 19.1 19",key:"10b0cb"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const mt=f("RefreshCw",[["path",{d:"M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8",key:"v9h5vc"}],["path",{d:"M21 3v5h-5",key:"1q7to0"}],["path",{d:"M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16",key:"3uifl3"}],["path",{d:"M8 16H3v5",key:"1cv678"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const Ct=f("Send",[["path",{d:"M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z",key:"1ffxy3"}],["path",{d:"m21.854 2.147-10.94 10.939",key:"12cjpa"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const Rt=f("ShieldCheck",[["path",{d:"M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z",key:"oel41y"}],["path",{d:"m9 12 2 2 4-4",key:"dzmm74"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const gt=f("TriangleAlert",[["path",{d:"m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3",key:"wmoenq"}],["path",{d:"M12 9v4",key:"juzpu7"}],["path",{d:"M12 17h.01",key:"p32p05"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const wt=f("WalletCards",[["rect",{width:"18",height:"18",x:"3",y:"3",rx:"2",key:"afitv7"}],["path",{d:"M3 9a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2",key:"4125el"}],["path",{d:"M3 11h3c.8 0 1.6.3 2.1.9l1.1.9c1.6 1.6 4.1 1.6 5.7 0l1.1-.9c.5-.5 1.3-.9 2.1-.9H21",key:"1dpki6"}]]);/**
 * @license lucide-react v0.468.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const Tt=f("X",[["path",{d:"M18 6 6 18",key:"1bl5f8"}],["path",{d:"m6 6 12 12",key:"d8bk6v"}]]);export{it as A,ct as C,lt as F,yt as G,dt as H,ht as M,vt as O,_t as P,st as R,Rt as S,gt as T,wt as W,Tt as X,E as a,mt as b,ut as c,Ct as d,Et as e,ft as f,J as g,kt as h,at as i,pt as j,et as r};
