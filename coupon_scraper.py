import argparse
import csv
import json
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

COUPON_CENTER_URL = "https://www.szlcsc.com/huodong.html"
DEFAULT_TIMEOUT = 15
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

SCRIPT_ROOT = Path(__file__).resolve().parent
BRAND_PRODUCT_SCRIPT = SCRIPT_ROOT / "scripts" / "fetch_brand_products.js"


def fetchCouponPage(url: str = COUPON_CENTER_URL, timeout: int = DEFAULT_TIMEOUT) -> str:
    request = Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        encoding = response.headers.get_content_charset() or "utf-8"
        html = response.read().decode(encoding, errors="replace")
    return html


def extractNextData(html: str) -> Dict[str, Any]:
    match = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise ValueError("未找到__NEXT_DATA__脚本块, 页面结构可能已变化")
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as error:
        raise ValueError("__NEXT_DATA__脚本块JSON解析失败") from error


def buildPartitionMap(nextData: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    pageProps = nextData.get("props", {}).get("pageProps", {})
    partitionList = pageProps.get("partitionList") or []
    partitionMap: Dict[str, Dict[str, Any]] = {}
    for item in partitionList:
        code = item.get("frontPartitionCode")
        if code:
            partitionMap[code] = item
    return partitionMap


def normalizeValidity(couponEntry: Dict[str, Any]) -> str:
    validType = couponEntry.get("couponValidType")
    if validType == "time_scope":
        begin = couponEntry.get("couponValidBeginTime") or ""
        end = couponEntry.get("couponValidEndTime") or ""
        validity = f"{begin} ~ {end}".strip(" ~")
        return validity
    if validType == "days":
        dayCount = couponEntry.get("couponValidDay")
        return f"领取后{dayCount}天内有效" if dayCount else ""
    return ""


def collectCoupons(nextData: Dict[str, Any]) -> List[Dict[str, Any]]:
    pageProps = nextData.get("props", {}).get("pageProps", {})
    couponData = pageProps.get("couponsDataList") or {}
    couponMap = couponData.get("couponModelVOListMap") or {}
    partitionMap = buildPartitionMap(nextData)
    couponList: List[Dict[str, Any]] = []
    seenUuid = set()
    for partitionCode, couponEntries in couponMap.items():
        partitionInfo = partitionMap.get(partitionCode, {})
        partitionName = partitionInfo.get("frontPartitionName") or partitionCode
        partitionSort = partitionInfo.get("frontPartitionSort", 9999)
        for couponEntry in couponEntries:
            uuid = couponEntry.get("uuid")
            if uuid and uuid in seenUuid:
                continue
            if uuid:
                seenUuid.add(uuid)
            couponList.append(
                {
                    "couponId": couponEntry.get("couponId"),
                    "uuid": uuid,
                    "partitionCode": partitionCode,
                    "partitionName": partitionName,
                    "partitionSort": partitionSort,
                    "couponName": couponEntry.get("couponName"),
                    "couponAmount": couponEntry.get("couponAmount"),
                    "couponLabelName": couponEntry.get("couponLabelName"),
                    "couponTypeName": couponEntry.get("couponTypeName"),
                    "couponActivityName": couponEntry.get("couponActivityName"),
                    "targetUrl": couponEntry.get("targetUrl"),
                    "grantLevel": couponEntry.get("grantLevel"),
                    "minOrderMoney": couponEntry.get("minOrderMoney"),
                    "limitBrandNames": couponEntry.get("limitBrandNames"),
                    "limitCatalogNames": couponEntry.get("limitCatalogNames"),
                    "customerMaxNum": couponEntry.get("customerMaxNum"),
                    "receiveCustomerNum": couponEntry.get("receiveCustomerNum"),
                    "validity": normalizeValidity(couponEntry),
                    "couponValidBeginTime": couponEntry.get("couponValidBeginTime"),
                    "couponValidEndTime": couponEntry.get("couponValidEndTime"),
                    "couponValidType": couponEntry.get("couponValidType"),
                    "couponValidDay": couponEntry.get("couponValidDay"),
                    "isReceive": couponEntry.get("isReceive"),
                    "isUse": couponEntry.get("isUse"),
                    "isInvalid": couponEntry.get("isInvalid"),
                }
            )
    couponList.sort(
        key=lambda item: (
            item.get("partitionSort", 9999),
            item.get("partitionName") or "",
            -(item.get("couponAmount") or 0),
            item.get("couponName") or "",
        )
    )
    return couponList


def filterCoupons(couponList: List[Dict[str, Any]], keywordList: Sequence[str]) -> List[Dict[str, Any]]:
    if not keywordList:
        return couponList
    normalizedKeywordList = [keyword.lower() for keyword in keywordList]
    filteredList: List[Dict[str, Any]] = []
    for coupon in couponList:
        couponName = coupon.get("couponName") or ""
        nameLower = couponName.lower()
        if any(keyword in nameLower for keyword in normalizedKeywordList):
            filteredList.append(coupon)
    return filteredList


def filterCouponsByValue(couponList: List[Dict[str, Any]], maxActualPay: float = 5.0, minCouponAmount: float = 10.0) -> List[Dict[str, Any]]:
    """
    过滤优惠券: 实际支付<=maxActualPay 且 券面额>=minCouponAmount
    实际支付 = 满减金额 - 券面额
    """
    filteredList: List[Dict[str, Any]] = []
    for coupon in couponList:
        couponAmount = coupon.get("couponAmount") or 0
        minOrderMoney = coupon.get("minOrderMoney") or 0
        
        # 计算实际支付金额
        actualPay = minOrderMoney - couponAmount
        
        # 应用过滤条件
        if actualPay <= maxActualPay and couponAmount >= minCouponAmount:
            filteredList.append(coupon)
    
    return filteredList


def fetchBrandProducts(targetUrl: str, timeout: int = 120) -> List[Dict[str, Any]]:
    if not targetUrl:
        return []
    if not BRAND_PRODUCT_SCRIPT.exists():
        raise FileNotFoundError(
            "未找到品牌商品抓取脚本 fetch_brand_products.js, 请确认 scripts 目录存在该文件"
        )

    process = subprocess.run(
        ["node", str(BRAND_PRODUCT_SCRIPT), targetUrl],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )

    if process.returncode != 0:
        stderrMessage = process.stderr.strip() or "抓取品牌商品失败"
        raise RuntimeError(stderrMessage)

    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"解析品牌商品数据失败: {error}") from error

    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(payload.get("error"))

    products = payload.get("products")
    if isinstance(products, list):
        return products
    return []


def dumpCouponJson(couponList: List[Dict[str, Any]], outputPath: str) -> None:
    with open(outputPath, "w", encoding="utf-8") as fileObject:
        json.dump(couponList, fileObject, ensure_ascii=False, indent=2)


def dumpCouponCsv(couponList: List[Dict[str, Any]], outputPath: str) -> None:
    """将优惠券列表输出为CSV文件"""
    if not couponList:
        # 如果没有数据，创建空文件
        with open(outputPath, "w", encoding="utf-8-sig", newline="") as fileObject:
            pass
        return
    
    # 定义CSV列
    fieldNames = [
        "partitionName",
        "couponName",
        "couponAmount",
        "minOrderMoney",
        "actualPay",
        "validity",
        "receiveCustomerNum",
        "customerMaxNum",
        "limitBrandNames",
        "targetUrl",
        "availableProducts",
    ]

    columnHeaders = {
        "partitionName": "分区",
        "couponName": "优惠券名称",
        "couponAmount": "面额(元)",
        "minOrderMoney": "满减金额(元)",
        "actualPay": "实际支付(元)",
        "validity": "有效期",
        "receiveCustomerNum": "领取人数",
        "customerMaxNum": "每人限领",
        "limitBrandNames": "品牌限制",
        "targetUrl": "链接",
        "availableProducts": "有货商品",
    }
    
    with open(outputPath, "w", encoding="utf-8-sig", newline="") as fileObject:
        writer = csv.DictWriter(fileObject, fieldnames=fieldNames)
        
        # 写入中文表头
        writer.writerow(columnHeaders)
        
        # 写入数据
        for coupon in couponList:
            couponAmount = coupon.get("couponAmount") or 0
            minOrderMoney = coupon.get("minOrderMoney") or 0
            actualPay = minOrderMoney - couponAmount
            productEntries = coupon.get("availableProducts") or []
            productSummary = " | ".join(
                f"{item.get('name')}<{item.get('link')}>"
                for item in productEntries
            )

            rowData = {
                "partitionName": coupon.get("partitionName") or "",
                "couponName": coupon.get("couponName") or "",
                "couponAmount": couponAmount,
                "minOrderMoney": minOrderMoney,
                "actualPay": actualPay,
                "validity": coupon.get("validity") or "",
                "receiveCustomerNum": coupon.get("receiveCustomerNum") or "",
                "customerMaxNum": coupon.get("customerMaxNum") or "",
                "limitBrandNames": coupon.get("limitBrandNames") or "无",
                "targetUrl": coupon.get("targetUrl") or "",
                "availableProducts": productSummary,
            }
            writer.writerow(rowData)


def renderCouponSummary(couponList: List[Dict[str, Any]]) -> None:
    if not couponList:
        print("未匹配到任何优惠券")
        return
    for index, coupon in enumerate(couponList, 1):
        amount = coupon.get("couponAmount")
        amountText = f"{amount}元" if amount is not None else "--"
        minMoney = coupon.get("minOrderMoney")
        minMoneyText = f"满{minMoney}元可用" if minMoney else "无门槛"
        brandLimit = coupon.get("limitBrandNames") or ""
        brandText = f"品牌限制: {brandLimit}" if brandLimit else "品牌限制: 无"
        validityText = coupon.get("validity") or "有效期未知"
        receiveCount = coupon.get("receiveCustomerNum")
        maxNum = coupon.get("customerMaxNum")
        print(f"{index:03d}. [{coupon.get('partitionName')}] {coupon.get('couponName')}")
        print(f"     面额: {amountText} | {minMoneyText}")
        print(f"     有效期: {validityText}")
        print(f"     领取人数: {receiveCount} | 每人限领: {maxNum}")
        print(f"     {brandText}")
        print(f"     链接: {coupon.get('targetUrl')}")
        availableProducts = coupon.get("availableProducts") or []
        if availableProducts:
            previewList = availableProducts[:3]
            productLines = [
                f"     - {item.get('name')} ({item.get('link')})"
                for item in previewList
            ]
            print("     有货商品:")
            for line in productLines:
                print(line)
            if len(availableProducts) > len(previewList):
                print(f"     ... 共 {len(availableProducts)} 个有货商品")
        elif coupon.get("productFetchError"):
            print(f"     有货商品: 获取失败 - {coupon.get('productFetchError')}")
        print("")


def runCrawler() -> None:
    argParser = argparse.ArgumentParser(description="抓取立创商城优惠券中心全部优惠券数据")
    argParser.add_argument("--keyword", "-k", action="append", dest="keywordList", help="根据优惠券名称关键词过滤, 可重复指定")
    argParser.add_argument("--json", dest="jsonPath", help="将结果写入指定JSON文件")
    argParser.add_argument("--csv", dest="csvPath", help="将结果写入指定CSV文件")
    argParser.add_argument("--limit", type=int, default=0, help="限制输出前N条结果")
    argParser.add_argument("--silent", action="store_true", help="仅抓取数据不在终端打印概要")
    argParser.add_argument("--url", default=COUPON_CENTER_URL, help="自定义优惠券页面URL")
    argParser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="请求超时时间(秒)")
    argParser.add_argument("--max-actual-pay", type=float, default=None, help="过滤: 实际支付金额上限(元)")
    argParser.add_argument("--min-coupon-amount", type=float, default=None, help="过滤: 券面额下限(元)")
    cliArgs = argParser.parse_args()

    try:
        html = fetchCouponPage(cliArgs.url, cliArgs.timeout)
        nextData = extractNextData(html)
        couponList = collectCoupons(nextData)
    except (HTTPError, URLError, socket.timeout) as error:
        print(f"请求优惠券页面失败: {error}", file=sys.stderr)
        sys.exit(1)
    except ValueError as error:
        print(f"解析优惠券数据失败: {error}", file=sys.stderr)
        sys.exit(1)

    keywordList = cliArgs.keywordList or []
    filteredList = filterCoupons(couponList, keywordList)
    
    # 应用价值过滤
    if cliArgs.max_actual_pay is not None or cliArgs.min_coupon_amount is not None:
        maxActualPay = cliArgs.max_actual_pay if cliArgs.max_actual_pay is not None else float('inf')
        minCouponAmount = cliArgs.min_coupon_amount if cliArgs.min_coupon_amount is not None else 0.0
        filteredList = filterCouponsByValue(filteredList, maxActualPay, minCouponAmount)
    
    if cliArgs.limit:
        filteredList = filteredList[: cliArgs.limit]

    productCache: Dict[str, List[Dict[str, Any]]] = {}
    for coupon in filteredList:
        targetUrl = coupon.get("targetUrl") or ""
        if not targetUrl:
            coupon["availableProducts"] = []
            continue
        if targetUrl not in productCache:
            try:
                products = fetchBrandProducts(targetUrl)
                products = [
                    {
                        "name": item.get("name"),
                        "link": item.get("link"),
                        "productCode": item.get("productCode"),
                        "stockText": item.get("stockText"),
                        "totalStock": item.get("totalStock"),
                        "warehouses": item.get("warehouses") or [],
                    }
                    for item in products
                    if item.get("hasStock")
                ]
            except Exception as error:  # pylint: disable=broad-except
                coupon["productFetchError"] = str(error)
                products = []
            productCache[targetUrl] = products
        coupon["availableProducts"] = productCache.get(targetUrl, [])

    if cliArgs.jsonPath:
        dumpCouponJson(filteredList, cliArgs.jsonPath)
    
    if cliArgs.csvPath:
        dumpCouponCsv(filteredList, cliArgs.csvPath)

    if not cliArgs.silent:
        print(f"共抓取优惠券: {len(couponList)} 条, 当前展示: {len(filteredList)} 条")
        renderCouponSummary(filteredList)


if __name__ == "__main__":
    runCrawler()
