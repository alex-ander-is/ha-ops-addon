/* Route callback failures belong to the outer owned-browser lifetime. */
export async function intercept(page,pattern,handle){
 let reject,closed=false;const pending=new Set();
 const failure=new Promise((_,fail)=>{reject=fail;});
 failure.catch(()=>{});
 const callback=async route=>{
  pending.add(route);
  try{
   const response=await route.fetch({timeout:120000});
   if(closed){await route.abort().catch(()=>{});return;}
   await handle(route,response);
  }catch(error){reject(error);await route.abort().catch(()=>{});}
 };
 await page.route(pattern,callback);
 return {
  guard:work=>Promise.race([Promise.resolve().then(work),failure]),
  async close(){closed=true;await page.unroute(pattern,callback);await Promise.all([...pending].map(route=>route.abort().catch(()=>{})));pending.clear();},
 };
}
