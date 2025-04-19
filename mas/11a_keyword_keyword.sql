SELECT
    keyword.keyword,
    save_circuit(provenance(), 'keyword_id', '{path}')
FROM
    conference, domain_conference, domain, domain_keyword, keyword
WHERE
    conference.cid = domain_conference.cid
    AND domain_conference.did = domain.did
    AND domain.did = domain_keyword.did
    AND domain_keyword.kid = keyword.kid
    AND domain.name = 'Databases'
GROUP BY keyword.keyword
LIMIT 10