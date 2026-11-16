import{reactive}from 'vue';
import{getChains,getChain}from '../api/chains';
import type{ChainGroup,ChainGroupDetail}from '../types/domain';
export const chains=reactive({items:[]as ChainGroup[],selected:null as ChainGroupDetail|null});
export async function loadChains(){chains.items=await getChains();}
export async function loadChain(id:number){chains.selected=await getChain(id);}
