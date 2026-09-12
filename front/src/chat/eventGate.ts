// 双通道（POST /chat 即时流 "post"、GET /stream EventSource "stream"）事件幂等门。
// 纯模块、不依赖 React：按 (turnId, eventSeq) 去重，gate 实例按会话生命周期持有，
// 实例之间状态互不共享。并维护会话级高水位 maxSeenSeq（含 notification 等所有带
// seq 事件），供调用方检测 seq 跳变缺口并触发一次静默历史重拉合并。

export type EventChannel = "post" | "stream";

export interface SequencedEvent {
  data: { turnId?: string | null; eventSeq?: number | null };
}

export interface IngestResult {
  /** true=首次见到，调用方应 applyEvent；false=重复或该通道应忽略，调用方跳过 */
  apply: boolean;
  /** true=新到 seq 相对会话高水位发生跳变（缺事件），调用方应安排静默重拉 */
  gap: boolean;
}

export interface EventGate {
  ingest(ev: SequencedEvent, channel: EventChannel): IngestResult;
  /** 会话级高水位：所有带有限 seq 的事件（含不渲染的 notification）共同推进；
   *  null=尚未见到任何有限 seq（首连重放起点 seq 很大也只建基线、不报缺口） */
  readonly maxSeenSeq: number | null;
}

export function createEventGate(): EventGate {
  // 去重游标：键为 `${turnId 缺省则空串}#${eventSeq}`。
  // notification 无 turnId 也占 seq，空串前缀保证其键不与 turn 事件塌缩。
  const seen = new Set<string>();
  let maxSeq: number | null = null;

  return {
    get maxSeenSeq() {
      return maxSeq;
    },

    ingest(ev, channel): IngestResult {
      const { turnId, eventSeq } = ev.data;
      // 防御分支：seq 必须是有限数字（NaN/Infinity/非数字一律按"无 seq"处理）。
      // 后端经 broker 投递的事件都带 seq，无 seq 属异常兜底：
      // post 通道放行但不记忆——无法构造稳定键，重复投递无法去重（设计接受）；
      // stream 端（含 EventSource 重放）一律忽略，避免历史形态事件冲进来；
      // 两条路径都不推进高水位。
      if (typeof eventSeq !== "number" || !Number.isFinite(eventSeq)) {
        return { apply: channel === "post", gap: false };
      }
      const key = `${turnId ?? ""}#${eventSeq}`;
      if (seen.has(key)) {
        return { apply: false, gap: false };
      }
      seen.add(key);
      const prevMax = maxSeq;
      // 乱序迟到（seq < 水位）只更新记忆、不报 gap；首连基线（prevMax===null）
      // 即使 seq 很大也不报缺口。
      const gap = prevMax !== null && eventSeq > prevMax + 1;
      maxSeq = prevMax === null ? eventSeq : Math.max(prevMax, eventSeq);
      return { apply: true, gap };
    },
  };
}
