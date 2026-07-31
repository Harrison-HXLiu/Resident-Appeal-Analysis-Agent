# 地图静态资源

- `china.json` 和 `vendor/echarts.min.js` 复用自仓库 `origin/dev-cwb` 分支已有的本地化地图成果。
- `admin-centers.json` 是 2026-07-30 从 DataV 行政区划公开接口
  `https://geo.datav.aliyun.com/areas_v3/bound/all.json` 获取的行政区中心点快照。

页面只读取本地静态文件，不依赖浏览器访问外部 CDN。研究团队确认来源平台归并表时，
仍应把地级市行政码和人工核定坐标写入 `regions` 表；前端中心点仅用于未核定坐标的
显示降级。
