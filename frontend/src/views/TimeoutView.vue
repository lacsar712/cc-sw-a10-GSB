<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api.js'

const router = useRouter()
const role = ref(localStorage.getItem('role') || '')
const settings = ref(null)
const jobs = ref([])
const logs = ref([])
const err = ref('')
const saving = ref(false)
const secondsInput = ref(30)
// 每秒自增，驱动倒计时重算
const tick = ref(0)
let timer
let countdownTimer

const canEdit = computed(() => role.value === 'writer')
const watched = computed(() => {
  // 依赖 tick，使倒计时在两次轮询之间也会刷新
  tick.value
  return jobs.value
    .filter((j) => j.status === 'claimed')
    .map((j) => {
      const deadline = new Date(j.heartbeat_at).getTime() + (j.timeout_seconds || 0) * 1000
      const remainMs = deadline - Date.now()
      return {
        ...j,
        remainMs,
        remainText: remainMs > 0 ? (remainMs / 1000).toFixed(1) + ' s' : '已超时，待回收',
        overdue: remainMs <= 0,
      }
    })
})

function fmtTime(s) {
  if (!s) return '—'
  const d = new Date(s)
  return d.toLocaleString('zh-CN', { hour12: false })
}

async function refresh() {
  if (!localStorage.getItem('tok')) return
  try {
    const [s, j, l] = await Promise.all([
      api('/api/settings'),
      api('/api/jobs'),
      api('/api/recycle-log'),
    ])
    settings.value = s
    jobs.value = j
    logs.value = l
    err.value = ''
  } catch (e) {
    err.value = String(e.message || e)
  }
}

async function saveSettings() {
  err.value = ''
  saving.value = true
  try {
    settings.value = await api('/api/settings', {
      method: 'PUT',
      body: JSON.stringify({ claim_timeout_seconds: Number(secondsInput.value) }),
    })
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    saving.value = false
  }
}

function syncInput() {
  if (settings.value) secondsInput.value = settings.value.claim_timeout_seconds
}

onMounted(() => {
  role.value = localStorage.getItem('role') || ''
  refresh().then(syncInput)
  timer = setInterval(async () => {
    await refresh()
    syncInput()
  }, 1000)
  countdownTimer = setInterval(() => {
    tick.value++
  }, 200)
})
onUnmounted(() => {
  clearInterval(timer)
  clearInterval(countdownTimer)
})
</script>

<template>
  <div class="timeout-page">
    <p v-if="err" class="err">{{ err }}</p>

    <!-- 一区：超时秒数 -->
    <section class="zone">
      <h3>① 超时秒数设置</h3>
      <div v-if="settings">
        <template v-if="canEdit">
          <label>
            领取心跳超时
            <input
              v-model.number="secondsInput"
              type="number"
              min="1"
              max="3600"
              style="width: 90px; margin: 0 6px"
            />
            秒
          </label>
          <button type="button" :disabled="saving" @click="saveSettings">保存</button>
          <p class="hint">
            当前 {{ settings.claim_timeout_seconds }} 秒（{{ settings.updated_by }}
            于 {{ fmtTime(settings.updated_at) }} 设置）。保存后只约束之后新进领取态的单，已领取的单仍按领取时的快照秒数计时。
          </p>
        </template>
        <template v-else>
          <p>当前领取心跳超时：<b>{{ settings.claim_timeout_seconds }} 秒</b></p>
          <p class="hint">巡检员仅可查看设置，修改秒数请联系校准员。</p>
        </template>
      </div>
    </section>

    <!-- 二区：监视窗 -->
    <section class="zone">
      <h3>② 领取态监视窗 <span class="badge">{{ watched.length }}</span></h3>
      <p v-if="!watched.length" class="hint">当前没有处于领取态的单。</p>
      <table v-else border="1" cellpadding="6" class="grid">
        <thead>
          <tr>
            <th>编号</th><th>灯种</th><th>领取时间</th><th>最近心跳</th>
            <th>快照秒数</th><th>剩余</th><th>拖住</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="j in watched" :key="j.id" :class="{ overdue: j.overdue }">
            <td>
              <a href="javascript:void(0)" @click="router.push(`/jobs/${j.id}`)">{{ j.id }}</a>
            </td>
            <td>{{ j.lamp }}</td>
            <td>{{ fmtTime(j.claimed_at) }}</td>
            <td>{{ fmtTime(j.heartbeat_at) }}</td>
            <td>{{ j.timeout_seconds }} 秒</td>
            <td :class="{ 'overdue-text': j.overdue }">{{ j.remainText }}</td>
            <td>{{ j.simulate_stall ? '是（心跳冻结）' : '否' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <!-- 三区：回收流水 -->
    <section class="zone">
      <h3>③ 回收流水 <span class="badge">{{ logs.length }}</span></h3>
      <p v-if="!logs.length" class="hint">暂无回收记录。</p>
      <table v-else border="1" cellpadding="6" class="grid">
        <thead>
          <tr>
            <th>流水号</th><th>任务</th><th>灯种</th><th>触发秒数</th>
            <th>原领取时间</th><th>回收时间</th><th>说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="l in logs" :key="l.id">
            <td>{{ l.id }}</td>
            <td>
              <a href="javascript:void(0)" @click="router.push(`/jobs/${l.job_id}`)">{{ l.job_id }}</a>
            </td>
            <td>{{ l.lamp }}</td>
            <td>{{ l.timeout_seconds }} 秒</td>
            <td>{{ fmtTime(l.claimed_at) }}</td>
            <td>{{ fmtTime(l.recycled_at) }}</td>
            <td>{{ l.reason }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>
</template>

<style scoped>
.timeout-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.zone {
  padding: 12px 14px;
  border: 1px solid #ccc;
  border-radius: 6px;
  background: #fff;
}
.zone h3 {
  margin: 0 0 10px;
}
.grid {
  border-collapse: collapse;
  width: 100%;
}
.badge {
  display: inline-block;
  min-width: 20px;
  padding: 0 6px;
  border-radius: 10px;
  background: #1a2332;
  color: #fff;
  font-size: 12px;
  text-align: center;
}
.hint {
  color: #666;
  font-size: 13px;
  margin: 8px 0 0;
}
.err {
  color: #b00020;
}
tr.overdue {
  background: #fdecea;
}
.overdue-text {
  color: #b00020;
  font-weight: 600;
}
</style>
