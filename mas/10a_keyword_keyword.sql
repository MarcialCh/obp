SELECT 
    keyword.keyword,
    save_circuit(provenance(), 'keyword_id', '{path}')
FROM 
    publication, publication_keyword, keyword
WHERE
    publication.title LIKE 'Making %'
    AND publication.pid = publication_keyword.pid
    AND publication_keyword.kid = keyword.kid
    AND publication.citation_num > 10
GROUP BY keyword.keyword
LIMIT 10