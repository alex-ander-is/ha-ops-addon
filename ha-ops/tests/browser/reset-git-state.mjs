// Reusable Playwright MCP browser_run_code function. Replace URL/ARTIFACT_DIR
// from the disposable fixture's ready JSON; this flow owns/closes only its tab.
async (page) => {
  const url = 'RESET_FIXTURE_URL';
  const artifacts = 'RESET_ARTIFACT_DIR';
  const tab = await page.context().newPage();
  const owned = [tab];
  const cdp = await tab.context().newCDPSession(tab);
  const {targetInfo} = await cdp.send('Target.getTargetInfo');
  await cdp.detach();
  const assert = (ok, message) => { if (!ok) throw Error(message); };
  const evidence = { ownedTabs: [targetInfo.targetId], rendered: true, shippedBundle: true, cases: [] };
  try {
    await tab.goto(url);
    const reset = tab.getByRole('button', {name:'Reset Git State', exact:true});
    await reset.waitFor();
    const post = async (value) => {const response=await tab.request.post(url + '__dev_harness__/reset-case',
      { form: {case: value} }); assert(response.ok(), `fixture ${value} failed: ${response.status()}`); return response;};
    for (const kind of ['unknown', 'apply', 'save']) {
      await post(kind); await tab.reload(); await reset.waitFor();
      await tab.waitForFunction(kind => document.querySelector('ha-ops-app')?.state?.active_operation?.command === kind, kind);
      assert(await reset.evaluate(el => el.disabled), `${kind} fence must disable Reset`);
      const style = await reset.evaluate(el => {const s=getComputedStyle(el);return {background:s.backgroundColor,color:s.color,border:s.borderColor};});
      assert(style.background === 'rgb(229, 231, 235)' && style.color === 'rgb(107, 114, 128)' && style.border === 'rgb(209, 213, 219)', 'disabled Reset must use pale gray background, muted text and border');
      evidence.cases.push({kind,disabled:true,style});
    }
    await post('legacy'); await tab.reload(); await reset.waitFor();
    await tab.waitForFunction(() => document.querySelector('ha-ops-app')?.state?.reset_recovery?.available === true);
    assert(!(await reset.evaluate(el => el.disabled)), 'exact legacy incident must enable Reset');
    await tab.screenshot({path: `${artifacts}/reset-legacy-enabled.png`, fullPage:true});
    await post('arm');
    await reset.click();
    const confirm = tab.getByRole('button', {name:'Confirm', exact:true}).filter({visible:true});
    await confirm.waitFor(); await confirm.click();
    await tab.waitForFunction(() => document.querySelector('ha-ops-app')?.state?.last_status === 'running');
    assert(await reset.evaluate(el => el.disabled), 'genuinely running Reset must disable Reset');
    evidence.cases.push({kind:'running',disabled:true});
    await tab.screenshot({path: `${artifacts}/reset-running-disabled.png`, fullPage:true});
    await post('release');
    await tab.waitForFunction(() => {
      const s=document.querySelector('ha-ops-app')?.state;
      return s?.last_status==='success' && s?.last_action==='reset_git_state' && !s.active_operation;
    }, null, {timeout:30000});
    const proof = await (await tab.request.get(url + '__dev_harness__/reset-proof')).json();
    assert(proof.main===proof.target && proof.remote_main===proof.target, 'main must match B locally/remotely');
    assert(proof.preserved===proof.original && proof.live_unchanged && !proof.dirty, 'A and live bytes must survive');
    assert(!proof.active && !proof.intent && proof.selections.every(paths=>paths.length===0), 'completion clears owned intent/drafts');
    await tab.reload();
    await tab.waitForFunction(() => document.querySelector('ha-ops-app')?.state?.last_status === 'success');
    await tab.screenshot({path: `${artifacts}/reset-recovered-reload.png`, fullPage:true});
    evidence.proof=proof;
    return evidence;
  } finally {
    // This exact tab was created above; user/shared tabs and profile are retained.
    for (const ownedTab of owned) await ownedTab.close();
  }
}
