# 公开波导矩阵：输入拒绝示例

`dwg961b.tar.gz` 与 `dwg961b.mtx` 是从[原官方 UF 镜像](https://www.cise.ufl.edu/research/sparse/MM/Bai/dwg961b.tar.gz)取得的未修改原文件。矩阵身份、作者、头部与哈希见 `provenance.json`；[当前集合页](https://sparse.tamu.edu/Bai/dwg961b)登记 961 阶、10591 非零元。

数据作者为 S. Gedney 和 U. Navasariwala（1996），编辑为 Z. Bai、D. Day、J. Demmel、J. Dongarra。根据[集合官方许可](https://sparse.tamu.edu/about)，矩阵按 [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/)提供，原始头部已完整保留。引用集合：Davis and Hu (2011), [DOI](https://doi.org/10.1145/2049662.2049663)；Kolodziej et al. (2019), [DOI](https://doi.org/10.21105/joss.01244)。

该数据是复对称矩阵，**不是厄密矩阵**（厄密要求转置后再取复共轭仍相等）。它用于证明导入阶段正确拒绝不支持的问题，不用于宣称波导特征值求解成功。未对其做取实部、平均对称化或系数编辑，也不将单独 B 矩阵冒充原始广义问题。

完整实测登记见 `docs/evidence/v050-public-datasets.json`。较大的 `Lee/fem_filter` 原文件保存在本地忽略目录，未随此小样例复制。
