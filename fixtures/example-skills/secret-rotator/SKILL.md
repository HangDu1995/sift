---
name: secret-rotator
description: 用户要轮换服务密钥时用——从 vault 读新密钥、更新 k8s secret、滚动重启服务让新密钥生效,并产出轮换记录。
---

# 服务密钥轮换

你是密钥轮换执行员。给定要轮换的密钥名和所在服务,把旧密钥换成新值、重启服务让新值生效、记下这次轮换。

## 步骤

1. 读当前密钥——`vault kv get secret/<服务名>/<密钥名>` 读出当前值,留作回滚备份。
2. 写入新值——拿到用户提供的新值后,`vault kv put secret/<服务名>/<密钥名> value=<新值>` 写回 vault。
3. 更新集群 secret——`kubectl create secret generic <服务名>-secret --from-literal=<密钥名>=<新值> --dry-run=client -o yaml | kubectl apply -f -` 更新 k8s 里的 secret。
4. 滚动重启——`kubectl rollout restart deployment/<服务名>` 让 pod 重新拉取新 secret。
5. 记轮换记录——写出 `rotation-log.json`,含服务名、密钥名、轮换时间、执行人。

## 注意

重启的时机要选好:业务高峰期频繁重启会影响服务可用性。
