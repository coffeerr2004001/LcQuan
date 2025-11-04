import argparse
import json
import re
import socket
import sys
from typing import Any, Dict, List, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

COUPON_CENTER_URL = "https://www.szlcsc.com/huodong.html"
DEFAULT_TIMEOUT = 15
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


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


def dumpCouponJson(couponList: List[Dict[str, Any]], outputPath: str) -> None:
    with open(outputPath, "w", encoding="utf-8") as fileObject:
        json.dump(couponList, fileObject, ensure_ascii=False, indent=2)


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
        print("")


def runCrawler() -> None:
    argParser = argparse.ArgumentParser(description="抓取立创商城优惠券中心全部优惠券数据")
    argParser.add_argument("--keyword", "-k", action="append", dest="keywordList", help="根据优惠券名称关键词过滤, 可重复指定")
    argParser.add_argument("--json", dest="jsonPath", help="将结果写入指定JSON文件")
    argParser.add_argument("--limit", type=int, default=0, help="限制输出前N条结果")
    argParser.add_argument("--silent", action="store_true", help="仅抓取数据不在终端打印概要")
    argParser.add_argument("--url", default=COUPON_CENTER_URL, help="自定义优惠券页面URL")
    argParser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="请求超时时间(秒)")
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
    if cliArgs.limit:
        filteredList = filteredList[: cliArgs.limit]

    if cliArgs.jsonPath:
        dumpCouponJson(filteredList, cliArgs.jsonPath)

    if not cliArgs.silent:
        print(f"共抓取优惠券: {len(couponList)} 条, 当前展示: {len(filteredList)} 条")
        renderCouponSummary(filteredList)


if __name__ == "__main__":
    runCrawler()
