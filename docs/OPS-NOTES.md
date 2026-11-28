# 运维调优笔记（Linux / k3s 实测）

> 记录踩过的系统层坑与验证过的结论，避免重复排查。
> 环境：阿里云 `aliyun-master`（2C2G / Debian / 内核 6.1.0）为主。

---

## 1. `sysctl.d` 会被 `sysctl.conf` 覆盖

**现象**：把 `vm.swappiness=20` 写进 `/etc/sysctl.d/99-swappiness.conf`，执行 `sysctl --system` 后仍然是旧值 `0`。

**原因**：`sysctl --system` 的加载顺序是

```
/run/sysctl.d/*.conf
/etc/sysctl.d/*.conf
/usr/lib/sysctl.d/*.conf
/etc/sysctl.conf          ← 最后加载，优先级最高
```

文件序号再大也压不过最后加载的 `/etc/sysctl.conf`。

**结论**：改内核参数时，**必须全盘搜索冲突项**：

```bash
grep -rn "vm.swappiness" /etc/sysctl.d /run/sysctl.d /usr/lib/sysctl.d /etc/sysctl.conf
```

改完用 `cat /proc/sys/vm/swappiness` 复核（不是只看 `sysctl -p` 的输出）。

---

## 2. `vm.swappiness` 的取值范围是 0~200（不是 0~10）

| 内核 | 范围 | 默认 |
|---|---|---|
| ≤ 5.7 | 0 ~ 100 | 60 |
| **≥ 5.8** | **0 ~ 200** | 60 |

语义在 5.8 重新标定：**新刻度 100 ≈ 旧刻度 60**（新 10 ≈ 旧 6）。

`0` **不是禁用 swap**，而是「尽量不换出匿名页，仅在内存真正告急时换出以避免 OOM」。真要禁用请用 `swapoff -a`。

**本机取值**：`20`（相当于旧刻度的 ~12，温和）。目标是在内存告急时提前、平滑腾挪，而不是临界点才猛换导致假死。

---

## 3. Kubernetes 到底支不支持 swap？

**支持，但默认不信任。** 演进：

| 版本 | 状态 |
|---|---|
| ≤ 1.21 | kubelet `failSwapOn: true`（默认），节点有 swap 就**拒绝启动** |
| 1.22 | `NodeSwap` alpha |
| 1.28 | beta |
| **1.30+** | **GA** |

`memorySwap.swapBehavior` 三种取值：

| 值 | 含义 |
|---|---|
| `NoSwap`（默认） | 容器**看不到** swap；宿主进程（kubelet/k3s/containerd/systemd）**仍可用** |
| `LimitedSwap` | 容器可用 swap，额度与内存请求挂钩 |
| `UnlimitedSwap` | 已废弃（仅 cgroup v1） |

**本机实况**（k3s v1.35.5）：

```
failSwapOn   = false        # 允许节点带 swap 运行
memorySwap   = {}           # 默认 NoSwap → 容器不碰 swap
/swapfile    = 2GB, Used 0
cgroupDriver = systemd (v2)
```

推论：调 `swappiness` 只影响**宿主进程**（本机 k3s-server 占 47% 内存，正是上次 API 假死的主因）；
若要让 Pod（如 argocd-controller）也能用 swap，需把 `swapBehavior` 改为 `LimitedSwap` 并重启 kubelet。

---

## 4. 阿里云 2C2G 控制面的内存画像（2026-11-28 实测）

| 进程 | RSS | 占比 |
|---|---|---|
| k3s-server（apiserver+etcd+scheduler+cm） | 789 MB | 47.0% |
| argocd-application-controller（管 32 个 Application） | 261 MB | 15.5% |
| containerd | 49 MB | 2.9% |
| argocd-server / repo-server | 47 / 44 MB | 5.4% |
| 其余（coredns/metrics-server/tailscaled/云盾…） | ~80 MB | 5% |
| **可用余量** | **~150 MB** | **9%** |

**症状与因果**：可用余量不足 10% 时，k3s 处理稍大对象即内存吃紧 → API 数十秒无响应 →
`kubectl` 挂起、ArgoCD 同步停滞（实测延迟约 40 分钟）→ 表现为「Application 没创建」的假象。

**改善优先级**：
1. `swappiness` 0 → 20（已做，零风险）
2. 清理/合并 ArgoCD Application（32 个偏多）
3. 升配 2G → 4G
4. ArgoCD 迁至资源更充裕的集群

---

## 5. 排查心法

| 看到的现象 | 先别下结论 | 应该先验证 |
|---|---|---|
| `kubectl` 超时 | "集群挂了" | SSH 是否通、API 端口是否监听、`curl /healthz` 实际耗时 |
| API 返回 401 | "证书坏了" | 401 常是**正常的未认证响应**，说明服务是活的 |
| `free` 显示 used 很高 | "内存泄漏" | 看 `MemAvailable` 与 `Swap`；再看 RSS Top 归属 |
| 配置不生效 | "参数写错了" | 是否有更高优先级的配置文件覆盖 |
