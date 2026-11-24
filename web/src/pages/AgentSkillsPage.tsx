import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { MessageSquareText, Search, Wrench } from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { AgentSkillsView } from '@/components/agent-skills-view';
import { AgentToolsDialog } from '@/components/agent-tools-dialog';
import { prefetchTools } from '@/api/tools';

/** 智能体：官方技能只读，自建技能可启停 / 查看详情 / 删除；工具目录在同一页的小窗查看。 */
export default function AgentSkillsPage() {
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [toolsOpen, setToolsOpen] = useState(false);

  // 进页面即预取工具清单（模块级缓存，只请求一次），点开小窗零等待
  useEffect(() => {
    prefetchTools();
  }, []);

  return (
    <PlatformShell
      title="智能体"
      description="查看平台提供的技能与工具目录，了解各能力的用途与启用状态；自建技能支持启停、查看详情与删除，也可通过对话创建。"
      actions={(
        <Button size="sm" onClick={() => navigate('/assistant')}>
          <MessageSquareText className="size-4" />
          通过对话创建技能
        </Button>
      )}
    >
      <AgentSkillsView
        query={query}
        leadingSlot={(
          <Button size="sm" variant="outline" onClick={() => setToolsOpen(true)}>
            <Wrench className="size-4" />
            工具
          </Button>
        )}
        searchSlot={(
          <div className="relative min-w-0 flex-1 sm:w-64 sm:flex-none">
            <Search className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索技能名称或说明"
              className="pl-8"
            />
          </div>
        )}
      />
      <AgentToolsDialog open={toolsOpen} onClose={() => setToolsOpen(false)} />
    </PlatformShell>
  );
}
