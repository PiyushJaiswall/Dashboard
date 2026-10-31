import axios from 'axios';
export const api = axios.create({baseURL: import.meta.env.VITE_API_URL || 'http://localhost:8000/api', timeout: 30000});
api.interceptors.request.use((config)=>{const token=localStorage.getItem('aurelia_token'); if(token) config.headers.Authorization=`Bearer ${token}`; return config;});
export const money=(n:number|null|undefined)=>n==null?'—':new Intl.NumberFormat('en-IN',{style:'currency',currency:'INR',maximumFractionDigits:0}).format(n);
export const number=(n:number|null|undefined, digits=2)=>n==null?'—':new Intl.NumberFormat('en-IN',{maximumFractionDigits:digits}).format(n);
