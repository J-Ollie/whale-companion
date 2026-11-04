"""Reference-counted retrieval boundary shared by personal and group memories."""
from contextvars import ContextVar

CAPABILITY=ContextVar('whale_memory_kb_capability',default=frozenset())
PRIVATE_PREFIXES=('WhaleMemoryPrivate-','WhaleGroupPrivate-','WhaleSharedPublic-')


def install(manager):
    current=manager.retrieve
    state=getattr(current,'_whale_memory_boundary',None)
    if state is None:
        state={'users':0,'original':current}
        async def boundary(query,kb_names,*args,**kwargs):
            private=frozenset(n for n in kb_names if n.startswith(PRIVATE_PREFIXES))
            if not private.issubset(CAPABILITY.get()):
                raise ValueError('私有记忆只能通过当前联系人或本群的记忆入口检索')
            return await state['original'](query,kb_names,*args,**kwargs)
        boundary._whale_memory_boundary=state
        state['wrapper']=boundary;manager.retrieve=boundary
    state['users']+=1;released=False
    def release():
        nonlocal released
        if released:return
        released=True;state['users']-=1
        if state['users']==0 and manager.retrieve is state['wrapper']:
            manager.retrieve=state['original']
    return release
