const {chromium}=require('playwright');
const path=require('node:path');const os=require('node:os');const fs=require('node:fs');const {pathToFileURL}=require('node:url');
const root=path.resolve(__dirname,'..');const out=fs.mkdtempSync(path.join(os.tmpdir(),'signal-lab-smoke-'));
(async()=>{const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});let assertions=0;
for(const width of [390,1280]){const page=await browser.newPage({viewport:{width,height:900}});await page.goto(pathToFileURL(path.join(root,'docs/signal-lab.html')).href);
await page.waitForSelector('#trusted .card');if(await page.locator('.card').count()!==4)throw Error('Expected 4 rendered cards');assertions++;
for(const persona of ['builder','operator','learner'])for(const topic of ['reliability','privacy','learning']){await page.selectOption('#persona',persona);await page.selectOption('#topic',topic);if(!(await page.locator('#selection').innerText()).includes(persona))throw Error('persona');if(await page.locator('.card').count()!==4)throw Error('cards');assertions+=2;}
await page.locator('#trusted .card button').first().click();if(!(await page.locator('#feedback').innerText()).includes('Selected from trusted'))throw Error('feedback');assertions++;
await page.locator('#neither').click();if(!(await page.locator('#feedback').innerText()).includes('Neither helped'))throw Error('neither');assertions++;
await page.locator('#reset').click();if(await page.locator('#persona').inputValue()!=='builder'||await page.locator('#topic').inputValue()!=='reliability')throw Error('reset');assertions++;
if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Horizontal overflow');assertions++;
await page.screenshot({path:path.join(out,'signal-lab-'+width+'.png'),fullPage:true});await page.close();}
console.log(assertions+' UI assertions passed across 390px and 1280px');console.log('Screenshots: '+out);await browser.close();})().catch(error=>{console.error(error);process.exit(1);});
