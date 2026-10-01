import * as fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { spawn } from 'node:child_process';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FileTokenStore } from '../src/token-store.js';

vi.mock('node:fs', async importOriginal => {
  const actual = await importOriginal<typeof import('node:fs')>();
  return { ...actual, renameSync: vi.fn(actual.renameSync), lstatSync: vi.fn(actual.lstatSync) };
});
const folders:string[]=[];
const tmp=()=>{const folder=fs.mkdtempSync(path.join(os.tmpdir(),'synthetic-store-'));folders.push(folder);return folder;};
const token=(index:number)=>({app_id:'cli_SYNTHETIC',user_open_id:`ou_SYNTHETIC${index}`,access_token:'SYNTHETIC_TOKEN'});
afterEach(()=>{vi.restoreAllMocks();for(const folder of folders.splice(0))fs.rmSync(folder,{recursive:true,force:true});});
describe('private atomic user token storage',()=>{
  it('retries when a competing writer releases the lock before inspection',async()=>{
    const file=path.join(tmp(),'tokens.json'),lock=file+'.lock';fs.mkdirSync(lock);
    const original=(await vi.importActual<typeof import('node:fs')>('node:fs')).lstatSync;
    vi.mocked(fs.lstatSync).mockImplementationOnce((value:any)=>original(value));
    vi.mocked(fs.lstatSync).mockImplementationOnce(()=>{
      fs.rmdirSync(lock);throw Object.assign(new Error('released'),{code:'ENOENT'});
    });
    new FileTokenStore(file).save(token(0));
    expect(new FileTokenStore(file).load('cli_SYNTHETIC','ou_SYNTHETIC0')).not.toBeNull();
  });
  it('creates private files under a permissive umask',()=>{
    const file=path.join(tmp(),'new','tokens.json');const before=process.umask(0);
    try{new FileTokenStore(file).save(token(0));}finally{process.umask(before);}
    expect(fs.statSync(file).mode&0o777).toBe(0o600);expect(fs.statSync(path.dirname(file)).mode&0o777).toBe(0o700);
  });
  it.each(['{broken','[]','{"tokens":[]}','{"tokens":{"bad":{}}}'])('preserves corrupt input %s',content=>{
    const file=path.join(tmp(),'tokens.json');fs.writeFileSync(file,content,{mode:0o600});const store=new FileTokenStore(file);
    expect(()=>store.save(token(0))).toThrow(/Corrupt/);expect(()=>store.remove('cli_SYNTHETIC','ou_SYNTHETIC0')).toThrow();
    expect(fs.readFileSync(file,'utf8')).toBe(content);expect(fs.existsSync(file+'.lock')).toBe(false);
  });
  it('does not silently chmod old unsafe files',()=>{
    const file=path.join(tmp(),'tokens.json');fs.writeFileSync(file,'{}',{mode:0o644});
    expect(()=>new FileTokenStore(file).save(token(0))).toThrow(/0600/);expect(fs.statSync(file).mode&0o777).toBe(0o644);
  });
  it('preserves the original on rename failure and releases resources',()=>{
    const folder=tmp(),file=path.join(folder,'tokens.json'),store=new FileTokenStore(file);store.save(token(0));
    const before=fs.readFileSync(file,'utf8');vi.mocked(fs.renameSync).mockImplementationOnce(()=>{throw new Error('SYNTHETIC_FAULT');});
    expect(()=>store.save(token(1))).toThrow('SYNTHETIC_FAULT');
    expect(fs.readFileSync(file,'utf8')).toBe(before);expect(fs.readdirSync(folder)).toEqual(['tokens.json']);store.save(token(1));
  });
  it('rejects symlinks without changing their targets',()=>{
    const folder=tmp(),file=path.join(folder,'tokens.json'),target=path.join(folder,'original');fs.writeFileSync(target,'{}',{mode:0o600});fs.symlinkSync(target,file);
    expect(()=>new FileTokenStore(file).save(token(0))).toThrow();expect(fs.readFileSync(target,'utf8')).toBe('{}');
  });
  it('serializes independent Node and Python writers through the same lock',async()=>{
    const file=path.join(tmp(),'tokens.json');const module=pathToFileURL(path.resolve('src/token-store.ts')).href;
    const nodeScript=`const {FileTokenStore}=await import(process.argv[1]);for(let i=0;i<5;i++)new FileTokenStore(process.argv[2]).save({app_id:'cli_SYNTHETIC',user_open_id:'ou_SYNTHETIC'+(Number(process.argv[3])*5+i),access_token:'SYNTHETIC_TOKEN'});`;
    const run=(command:string,args:string[])=>new Promise<void>((resolve,reject)=>{const child=spawn(command,args,{env:{...process.env,PYTHONPATH:path.resolve('src')},stdio:'pipe'});let errors='';child.stderr.on('data',data=>errors+=data);child.on('error',reject);child.on('close',code=>code===0?resolve():reject(new Error(errors||`child exit ${code}`)));});
    const jobs=Array.from({length:3},(_,index)=>run(process.execPath,['--experimental-strip-types','--input-type=module','-e',nodeScript,module,file,String(index)]));
    const python=`import sys\nfrom feishu_auth_kit.token_store import FileTokenStore,StoredUserToken\nfor i in range(5): FileTokenStore(sys.argv[1]).save(StoredUserToken('cli_SYNTHETIC','ou_SYNTHETIC'+str(15+i),'SYNTHETIC_TOKEN'))`;
    jobs.push(run(process.env.FEISHU_TEST_PYTHON??'python3',['-c',python,file]));await Promise.all(jobs);
    expect(Object.keys(JSON.parse(fs.readFileSync(file,'utf8')).tokens)).toHaveLength(20);
  },10000);
});
