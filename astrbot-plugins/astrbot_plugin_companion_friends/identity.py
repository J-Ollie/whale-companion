"""Read one applicant's public QQ identity via the existing gateway.

The only wire operation is the UID-form profile lookup OIDB 0xFE1_2.
No arbitrary packet, model tool, account mutation or friend acceptance is exposed.
Field definitions follow SnowLuma's documented QQ profile protocol sources.
"""
import asyncio
import re


UID = re.compile(r'u_[A-Za-z0-9_-]{10,80}')
COMMAND = 'OidbSvcTrpcTcp.0xfe1_2'


def varint(value):
    out=bytearray()
    while value>127:
        out.append((value&127)|128);value>>=7
    out.append(value)
    return bytes(out)


def number(field,value):return varint(field<<3)+varint(value)


def blob(field,value):return varint((field<<3)|2)+varint(len(value))+value


def fields(data):
    if not isinstance(data,bytes) or len(data)>262144:raise ValueError('invalid_profile_size')
    result={};pos=0;count=0
    def read_int():
        nonlocal pos
        value=0
        for shift in range(0,70,7):
            if pos>=len(data):raise ValueError('truncated_profile')
            current=data[pos];pos+=1;value|=(current&127)<<shift
            if current<128:return value
        raise ValueError('invalid_profile_integer')
    while pos<len(data):
        tag=read_int();field,wire=tag>>3,tag&7;count+=1
        if field<1 or count>4096:raise ValueError('invalid_profile_fields')
        if wire==0:value=read_int()
        elif wire in (1,2,5):
            length=read_int() if wire==2 else (8 if wire==1 else 4)
            if pos+length>len(data):raise ValueError('truncated_profile_field')
            value=data[pos:pos+length];pos+=length
        else:raise ValueError('unsupported_profile_wire')
        result.setdefault(field,[]).append((wire,value))
    return result


def one(message,field,wire,default=None):
    values=message.get(field,[])
    if not values:return default
    if len(values)!=1 or values[0][0]!=wire:raise ValueError('ambiguous_profile_field')
    return values[0][1]


def request(uid):
    if not isinstance(uid,str) or UID.fullmatch(uid) is None:raise ValueError('unsupported_request_handle')
    # Ask only for nickname; the response identity carries UID and numeric QQ.
    body=blob(1,uid.encode())+blob(3,number(1,20002))
    # reserved=0 is the UID form. The numeric-account variant uses reserved=1.
    return number(1,0xfe1)+number(2,2)+blob(4,body)


def decode(uid,raw):
    if not isinstance(raw,str) or len(raw)>524288 or len(raw)%2 or re.fullmatch(r'[0-9a-fA-F]*',raw) is None:
        raise ValueError('invalid_profile_response')
    envelope=fields(bytes.fromhex(raw))
    if one(envelope,1,0,0xfe1)!=0xfe1 or one(envelope,2,0,2)!=2:raise ValueError('wrong_profile_service')
    if one(envelope,3,0,0)!=0:raise ValueError('profile_lookup_rejected')
    response=fields(one(envelope,4,2,b''));identity=fields(one(response,1,2,b''))
    echoed=one(identity,1,2)
    if echoed is not None and echoed.decode('utf-8')!=uid:raise ValueError('profile_uid_mismatch')
    qq=one(identity,3,0,0)
    if not 10000<=qq<=4294967295:raise ValueError('profile_account_missing')
    nickname='';properties=fields(one(identity,2,2,b''))
    for wire,item in properties.get(2,[]):
        if wire!=2:continue
        prop=fields(item)
        if one(prop,1,0)==20002:
            nickname=one(prop,2,2,b'').decode('utf-8')
            nickname=''.join(c for c in nickname if ord(c)>=32)[:48]
            break
    return {'qq':str(qq),'nickname':nickname,'source':'qq_uid_profile'}


async def resolve(call,uid):
    payload=request(uid)
    raw=await asyncio.wait_for(call('send_packet',cmd=COMMAND,data=payload.hex(),rsp=True),timeout=10)
    return decode(uid,raw)
